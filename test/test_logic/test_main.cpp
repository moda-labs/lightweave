// Host-side unit tests for the sync core and pattern math — the subtle, silently-
// failing logic the brief flags as "the hard part and the real risk". These run
// on your machine via `pio test -e native`; they need no ESP32 hardware.
//
// What is intentionally NOT tested here: radio range, real-world packet loss,
// the ADC2-dies-with-radio trap, and on-chip timing jitter. Those only surface
// on hardware — these tests complement field testing, they don't replace it.

#include <unity.h>

#include "beacon.h"
#include "sync.h"
#include "bootplan.h"
#include "dusk.h"
#include "keepalive.h"
#include "macaddr.h"
#include "napsched.h"
#include "pattern_ids.h"
#include "pattern_math.h"
#include "power_table.h"
#include "powermon.h"
#include "power_policy.h"
#include "powersave.h"
#include "roster.h"
#include "serial_json.h"
#include "table.h"
#include "table_wire.h"

// ---- Sync: locking & offset --------------------------------------------------

void test_starts_unlocked() {
  SyncState s;
  syncInit(s);
  TEST_ASSERT_FALSE(s.locked);
  TEST_ASSERT_EQUAL_INT64(0, s.offset_us);
  // Before any lock, synced time is just local time.
  TEST_ASSERT_EQUAL_INT64(12345, syncedTime(s, 12345));
}

void test_offset_reproduces_conductor_clock() {
  SyncState s;
  syncInit(s);
  // Conductor's clock is 1_000_000us ahead of ours when the beacon arrives.
  syncOnBeacon(s, /*epoch*/ 5'000'000, /*seq*/ 0, /*local*/ 4'000'000);
  TEST_ASSERT_TRUE(s.locked);
  TEST_ASSERT_EQUAL_INT64(1'000'000, s.offset_us);
  // syncedTime now maps a later local clock onto the conductor's timeline.
  TEST_ASSERT_EQUAL_INT64(6'000'000, syncedTime(s, 5'000'000));
}

void test_first_fix_snaps_exactly() {
  // The very first beacon has no coasting clock to protect, so it is adopted
  // exactly regardless of how large the implied offset is.
  SyncState s;
  syncInit(s);
  syncOnBeacon(s, 5'000'000, 0, 4'000'000);      // offset +1_000_000
  TEST_ASSERT_EQUAL_INT64(1'000'000, s.offset_us);
}

void test_small_correction_applies_in_full() {
  // A correction smaller than the slew cap is invisible already, so it is applied
  // whole — the clock tracks the conductor tightly under normal jitter/drift.
  SyncState s;
  syncInit(s);
  syncOnBeacon(s, 5'000'000, 0, 4'000'000);      // lock, offset +1_000_000
  // Next beacon implies +1_000_500 (a 500us nudge, < the 2ms cap): applied in full.
  syncOnBeacon(s, 9'000'500, 1, 8'000'000);
  TEST_ASSERT_EQUAL_INT64(1'000'500, s.offset_us);
}

void test_large_correction_slews_not_steps() {
  // A correction bigger than the cap but inside the gate glides over several
  // beacons instead of stepping — no visible jump in the animation.
  SyncState s;
  syncInit(s);
  syncOnBeacon(s, 5'000'000, 0, 4'000'000);      // lock, offset +1_000_000
  // Implied offset +1_010_000 (+10ms), gate is 100ms so it's trusted, but the cap
  // is 2ms/beacon, so we move exactly +2ms this beacon.
  syncOnBeacon(s, 9'010'000, 1, 8'000'000);
  TEST_ASSERT_EQUAL_INT64(1'002'000, s.offset_us);
  TEST_ASSERT_FALSE(s.reject_streak);
}

void test_delayed_beacon_is_gated_out() {
  // A beacon delayed by hundreds of ms reads a wildly wrong offset. The gate keeps
  // the coasting clock untouched so the whole field stays on the shared timeline.
  SyncState s;
  syncInit(s);
  syncOnBeacon(s, 5'000'000, 0, 4'000'000);      // lock, offset +1_000_000
  // This beacon arrived 800ms late: local is inflated, implied offset craters.
  BeaconOutcome o = syncOnBeacon(s, 9'000'000, 1, 8'800'000);  // implies +200_000
  TEST_ASSERT_TRUE(o.rejected);
  TEST_ASSERT_FALSE(o.relocked);
  TEST_ASSERT_EQUAL_INT64(1'000'000, s.offset_us);             // unchanged
  TEST_ASSERT_EQUAL_UINT32(1, s.offset_rejects);
  // A following on-time beacon is trusted again and clears the streak.
  BeaconOutcome ok = syncOnBeacon(s, 10'000'050, 2, 9'000'000);  // implies +1_000_050
  TEST_ASSERT_FALSE(ok.rejected);
  TEST_ASSERT_EQUAL_INT64(1'000'050, s.offset_us);
  TEST_ASSERT_EQUAL_UINT32(0, s.reject_streak);
}

void test_gated_beacon_still_counts_as_delivered() {
  // Delivery accounting (beacons_rx, seq/gap, last_beacon_us) tracks the radio and
  // must advance even when a beacon's timestamp is distrusted.
  SyncState s;
  syncInit(s);
  syncOnBeacon(s, 5'000'000, 0, 4'000'000);
  syncOnBeacon(s, 9'000'000, 1, 8'800'000);      // gated timestamp
  TEST_ASSERT_EQUAL_UINT32(2, s.beacons_rx);
  TEST_ASSERT_EQUAL_UINT32(0, s.seq_gaps);       // seq 0->1 is in order
  // last_beacon_us advanced to the gated beacon's arrival, so age is measured from it.
  TEST_ASSERT_EQUAL_INT64(200'000, beaconAge(s, 9'000'000));
  TEST_ASSERT_FALSE(syncIsStale(s, 9'000'000, 2'000'000));
}

void test_persistent_offset_shift_forces_relock() {
  // A real conductor jump (reboot / master change) makes EVERY beacon exceed the
  // gate. After relock_after (8) consecutive rejects the node adopts the new clock,
  // so a rebooted conductor doesn't strand the field on a dead timeline forever.
  SyncState s;
  syncInit(s);
  syncOnBeacon(s, 5'000'000, 0, 4'000'000);      // lock, offset +1_000_000
  // Conductor rebooted: its epoch is now ~0, implying a huge negative offset.
  BeaconOutcome o{false, false, false};
  for (uint32_t i = 1; i <= 8; i++) {
    o = syncOnBeacon(s, /*epoch*/ 1'000 * i, /*seq*/ i, /*local*/ 9'000'000 + i);
  }
  TEST_ASSERT_TRUE(o.relocked);                  // the 8th reject snaps
  TEST_ASSERT_EQUAL_UINT32(0, s.reject_streak);  // streak cleared on re-lock
  // Offset now reflects the new (rebooted) conductor, not the stale +1_000_000.
  TEST_ASSERT_TRUE(s.offset_us < 0);
  TEST_ASSERT_EQUAL_UINT32(8, s.offset_rejects);
}

// ---- Sync: free-run on missed beacons (must never blank) ---------------------

void test_free_run_keeps_advancing_without_beacons() {
  SyncState s;
  syncInit(s);
  syncOnBeacon(s, 5'000'000, 0, 4'000'000);  // offset +1_000_000
  // No further beacons. Synced time must keep advancing with the local clock,
  // not freeze or reset — this is the no-blackout guarantee.
  TEST_ASSERT_EQUAL_INT64(11'000'000, syncedTime(s, 10'000'000));
  TEST_ASSERT_EQUAL_INT64(31'000'000, syncedTime(s, 30'000'000));
}

void test_staleness_boundary() {
  SyncState s;
  syncInit(s);
  TEST_ASSERT_TRUE(syncIsStale(s, 0, 2'000'000));  // never locked => stale

  syncOnBeacon(s, 0, 0, 1'000'000);  // locked at local t=1_000_000
  // Just inside the window: not stale.
  TEST_ASSERT_FALSE(syncIsStale(s, 1'000'000 + 2'000'000, 2'000'000));
  // One microsecond past the window: stale (but still free-running).
  TEST_ASSERT_TRUE(syncIsStale(s, 1'000'000 + 2'000'001, 2'000'000));
}

void test_beacon_age() {
  SyncState s;
  syncInit(s);
  TEST_ASSERT_EQUAL_INT64(-1, beaconAge(s, 999));  // sentinel before lock
  syncOnBeacon(s, 0, 0, 1'000'000);
  TEST_ASSERT_EQUAL_INT64(2'500'000, beaconAge(s, 3'500'000));
}

// ---- Sync: drop / out-of-order detection ------------------------------------

void test_in_sequence_has_no_gaps() {
  SyncState s;
  syncInit(s);
  for (uint32_t i = 0; i < 100; i++) syncOnBeacon(s, i * 1000, i, i * 1000);
  TEST_ASSERT_EQUAL_UINT32(0, s.seq_gaps);
  TEST_ASSERT_EQUAL_UINT32(100, s.beacons_rx);
}

void test_dropped_beacon_counts_one_gap() {
  SyncState s;
  syncInit(s);
  syncOnBeacon(s, 0, 0, 0);
  syncOnBeacon(s, 0, 1, 0);
  BeaconOutcome o = syncOnBeacon(s, 0, 3, 0);  // skipped seq 2
  TEST_ASSERT_TRUE(o.gap);
  TEST_ASSERT_EQUAL_UINT32(1, s.seq_gaps);
}

void test_first_beacon_is_never_a_gap() {
  SyncState s;
  syncInit(s);
  // A node may join mid-stream; its first-ever beacon (any seq) is not a gap.
  BeaconOutcome o = syncOnBeacon(s, 0, 9999, 0);
  TEST_ASSERT_FALSE(o.gap);
  TEST_ASSERT_EQUAL_UINT32(0, s.seq_gaps);
}

void test_seq_gap_handles_uint32_wrap() {
  SyncState s;
  syncInit(s);
  syncOnBeacon(s, 0, 0xFFFFFFFEu, 0);          // lock at next-to-max
  BeaconOutcome a = syncOnBeacon(s, 0, 0xFFFFFFFFu, 0);  // +1: fine
  BeaconOutcome b = syncOnBeacon(s, 0, 0x00000000u, 0);  // wraps to 0: fine
  TEST_ASSERT_FALSE(a.gap);
  TEST_ASSERT_FALSE(b.gap);
  TEST_ASSERT_EQUAL_UINT32(0, s.seq_gaps);
}

// ---- Pattern math: phase wrap & pulse continuity ----------------------------

void test_phase_range_and_wrap() {
  // Phase stays in [0,1) and resets at the period boundary.
  TEST_ASSERT_FLOAT_WITHIN(1e-5, 0.0f, pmath::phase(0, 4.0f));
  TEST_ASSERT_FLOAT_WITHIN(1e-5, 0.5f, pmath::phase(2'000'000, 4.0f));
  TEST_ASSERT_FLOAT_WITHIN(1e-5, 0.0f, pmath::phase(4'000'000, 4.0f));   // wrapped
  TEST_ASSERT_FLOAT_WITHIN(1e-5, 0.25f, pmath::phase(5'000'000, 4.0f));  // 2nd period
}

void test_phase_handles_large_time_no_overflow() {
  // ~5 days of microseconds — must still be a clean phase, not garbage.
  int64_t t = (int64_t)5 * 24 * 3600 * 1'000'000;
  float p = pmath::phase(t, 4.0f);
  TEST_ASSERT_TRUE(p >= 0.0f && p < 1.0f);
}

void test_pulse_intensity_bounds_and_endpoints() {
  // Raised cosine: 0 at the start of a period, peak 1 at the half period.
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.0f, pmath::pulseIntensity(0, 4.0f, 0.0f));
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 1.0f, pmath::pulseIntensity(2'000'000, 4.0f, 0.0f));
  // Stays within [0,1] sampled across a full period.
  for (int64_t us = 0; us < 4'000'000; us += 50'000) {
    float v = pmath::pulseIntensity(us, 4.0f, 0.0f);
    TEST_ASSERT_TRUE(v >= -1e-4f && v <= 1.0f + 1e-4f);
  }
}

void test_pulse_continuous_across_wrap() {
  // No visible "hitch": the value just before the boundary ~= just after.
  float before = pmath::pulseIntensity(3'999'000, 4.0f, 0.0f);
  float after = pmath::pulseIntensity(4'001'000, 4.0f, 0.0f);
  TEST_ASSERT_FLOAT_WITHIN(0.01f, before, after);
}

// ---- Sweep: traveling wave across the field ---------------------------------

void test_sweep_bounds() {
  for (int64_t us = 0; us < 8'000'000; us += 37'000)
    for (float x = 0; x <= 5.0f; x += 0.5f) {
      float v = pmath::sweepIntensity(us, x, 4.0f, 3.0f);
      TEST_ASSERT_TRUE(v >= -1e-4f && v <= 1.0f + 1e-4f);
    }
}

void test_sweep_travels_with_position() {
  // A node at position x sees the same waveform as x=0, delayed by
  // period * x / wavelength. Here: period=4s, wavelength=3 -> delay for x=1.5
  // is 4 * 1.5/3 = 2.0s.
  const float period = 4.0f, wl = 3.0f, x = 1.5f;
  int64_t delay_us = (int64_t)(period * x / wl * 1e6);  // 2.0s
  for (int64_t t = 0; t < 4'000'000; t += 250'000) {
    float at_origin = pmath::sweepIntensity(t, 0.0f, period, wl);
    float at_x = pmath::sweepIntensity(t + delay_us, x, period, wl);
    TEST_ASSERT_FLOAT_WITHIN(1e-3, at_origin, at_x);
  }
}

void test_sweep_nodes_differ_in_phase() {
  // At a single instant, two nodes a half-wavelength apart are in opposition.
  // At t=2s (period 4s) node 0 is at its peak (1.0); the node 1.5 units away
  // (wavelength 3) is at its trough (0.0).
  int64_t t = 2'000'000;
  float a = pmath::sweepIntensity(t, 0.0f, 4.0f, 3.0f);
  float b = pmath::sweepIntensity(t, 1.5f, 4.0f, 3.0f);  // half a wavelength away
  TEST_ASSERT_TRUE(fabsf(a - b) > 0.9f);
}

// ---- Palette drift: rainbow hue cycle + HSV ---------------------------------

void test_hsv_primary_hues() {
  float r, g, b;
  pmath::hsvToRgb(0.0f, 1, 1, r, g, b);  // red
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 1, r);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0, g);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0, b);
  pmath::hsvToRgb(1.0f / 3, 1, 1, r, g, b);  // green
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0, r);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 1, g);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0, b);
  pmath::hsvToRgb(2.0f / 3, 1, 1, r, g, b);  // blue
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0, r);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0, g);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 1, b);
}

void test_hsv_red_to_yellow_passes_through_orange() {
  // The asked-for gradient: red -> orange -> yellow must be smooth. Halfway from
  // red (h=0) to yellow (h=1/6) is h=1/12, where green is ramping through ~0.5
  // with red full and blue zero == orange.
  float r, g, b;
  pmath::hsvToRgb(1.0f / 12, 1, 1, r, g, b);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 1.0f, r);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.5f, g);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.0f, b);
}

void test_hsv_wraps_and_stays_in_gamut() {
  // Any hue (including negative / >1) yields in-range RGB.
  for (float h = -1.0f; h <= 2.0f; h += 0.013f) {
    float r, g, b;
    pmath::hsvToRgb(h, 1, 1, r, g, b);
    TEST_ASSERT_TRUE(r >= -1e-4f && r <= 1.0f + 1e-4f);
    TEST_ASSERT_TRUE(g >= -1e-4f && g <= 1.0f + 1e-4f);
    TEST_ASSERT_TRUE(b >= -1e-4f && b <= 1.0f + 1e-4f);
  }
  // h and h+1 are the same color (the wheel wraps).
  float r0, g0, b0, r1, g1, b1;
  pmath::hsvToRgb(0.2f, 1, 1, r0, g0, b0);
  pmath::hsvToRgb(1.2f, 1, 1, r1, g1, b1);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, r0, r1);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, g0, g1);
  TEST_ASSERT_FLOAT_WITHIN(1e-4, b0, b1);
}

void test_color_value_metadata_round_trips_without_losing_zero() {
  uint16_t standard = pmath::colorValuePack(0);
  TEST_ASSERT_TRUE(pmath::colorValuePresent(standard));
  TEST_ASSERT_FLOAT_WITHIN(1e-6, 0.0f, pmath::colorValueDecode(standard));
  TEST_ASSERT_FLOAT_WITHIN(1e-6, 128.0f / 255.0f,
                           pmath::colorValueDecode(pmath::colorValuePack(128)));

  uint16_t firefly = pmath::fireflyMetaPack(73, 41);
  TEST_ASSERT_TRUE(pmath::colorValuePresent(firefly));
  TEST_ASSERT_EQUAL_UINT8(73, pmath::fireflyScatterDecode(firefly));
  TEST_ASSERT_FLOAT_WITHIN(1e-6, 41.0f / 255.0f,
                           pmath::fireflyValueDecode(firefly));

  uint16_t ocean_ws = pmath::oceanWavelengthSaturationPack(375, 37);
  uint16_t ocean_av = pmath::oceanAngleValuePack(205, 96);
  TEST_ASSERT_TRUE(pmath::oceanColorPresent(ocean_av));
  TEST_ASSERT_EQUAL_UINT16(375, pmath::oceanWavelengthDecode(ocean_ws, ocean_av));
  TEST_ASSERT_EQUAL_UINT16(205, pmath::oceanAngleDecode(ocean_av));
  TEST_ASSERT_FLOAT_WITHIN(0.02f, 0.37f,
                           pmath::oceanSaturationDecode(ocean_ws, ocean_av));
  TEST_ASSERT_FLOAT_WITHIN(0.02f, 96.0f / 255.0f,
                           pmath::oceanValueDecode(ocean_av));
}

void test_srgb_gamma_and_rgbw_extraction_preserve_hex_distinctions() {
  TEST_ASSERT_FLOAT_WITHIN(1e-6, 0.0f, pmath::srgbToLinear(0.0f));
  TEST_ASSERT_FLOAT_WITHIN(1e-6, 1.0f, pmath::srgbToLinear(1.0f));
  TEST_ASSERT_FLOAT_WITHIN(1e-3, 0.216f, pmath::srgbToLinear(128.0f / 255.0f));

  pmath::RgbwUnit bright = pmath::hsvToRgbw(32.0f / 360.0f, 1.0f, 1.0f, 1.0f);
  pmath::RgbwUnit half = pmath::hsvToRgbw(32.0f / 360.0f, 1.0f,
                                         128.0f / 255.0f, 1.0f);
  TEST_ASSERT_TRUE(half.r < bright.r * 0.3f);
  TEST_ASSERT_TRUE(half.g < bright.g * 0.3f);

  pmath::RgbwUnit gray = pmath::hsvToRgbw(0.0f, 0.0f, 128.0f / 255.0f, 1.0f);
  TEST_ASSERT_FLOAT_WITHIN(1e-6, 0.0f, gray.r);
  TEST_ASSERT_FLOAT_WITHIN(1e-6, 0.0f, gray.g);
  TEST_ASSERT_FLOAT_WITHIN(1e-6, 0.0f, gray.b);
  TEST_ASSERT_FLOAT_WITHIN(1e-3, 0.216f, gray.w);

  // #EEEE9B is chromatic (a pale yellow), not neutral white.  Sending its
  // common RGB component to the strip's warm-white die washes the tint out on
  // real hardware because that emitter is neither color- nor intensity-matched
  // to an RGB white mix.
  pmath::RgbwUnit pale_yellow =
      pmath::hsvToRgbw(60.0f / 360.0f, 35.0f / 100.0f,
                       238.0f / 255.0f, 1.0f);
  TEST_ASSERT_FLOAT_WITHIN(1e-6, 0.0f, pale_yellow.w);
  TEST_ASSERT_TRUE(pale_yellow.r > pale_yellow.b);
  TEST_ASSERT_FLOAT_WITHIN(1e-6, pale_yellow.r, pale_yellow.g);
}

void test_drift_hue_cycles_in_range() {
  for (int64_t us = 0; us < 8'000'000; us += 53'000) {
    float h = pmath::driftHue(us, 0.0f, 8.0f, 0.0f);
    TEST_ASSERT_TRUE(h >= 0.0f && h < 1.0f);
  }
}

void test_drift_hue_unison_by_default_but_travels_with_spatial() {
  // spatial=0: position is irrelevant, every node shares one hue.
  TEST_ASSERT_FLOAT_WITHIN(1e-5, pmath::driftHue(1'000'000, 0.0f, 8.0f, 0.0f),
                           pmath::driftHue(1'000'000, 5.0f, 8.0f, 0.0f));
  // spatial>0: hue is offset by position -> a traveling rainbow. x=1 is a
  // quarter-wheel ahead of x=0 when spatial=0.25.
  float at0 = pmath::driftHue(0, 0.0f, 8.0f, 0.25f);
  float at1 = pmath::driftHue(0, 1.0f, 8.0f, 0.25f);
  TEST_ASSERT_FLOAT_WITHIN(1e-5, 0.25f, at1 - at0);
}

// ---- Firefly ("hotaru") flash --------------------------------------------

void test_firefly_intensity_stays_in_gamut() {
  // Sweep time and a spread of positions; intensity must never leave [0,1].
  for (int64_t us = 0; us < 14'000'000; us += 37'000) {
    for (float x = 0.0f; x <= 1.0f; x += 0.13f) {
      for (float y = 0.0f; y <= 1.0f; y += 0.17f) {
        float v = pmath::fireflyIntensity(us, x, y, 7.0f, 1.0f);
        TEST_ASSERT_TRUE(v >= 0.0f && v <= 1.0f);
      }
    }
  }
}

void test_firefly_lit_most_of_cycle() {
  // The lantern is lit for most of each cycle now, dark only briefly.
  const float period = 7.0f;
  int lit = 0, total = 0;
  for (int64_t us = 0; us < (int64_t)(period * 1e6); us += 20'000) {
    total++;
    if (pmath::fireflyIntensity(us, 0.0f, 0.0f, period, 0.0f) > 0.0f) lit++;
  }
  TEST_ASSERT_TRUE(lit > total * 3 / 5);  // lit more than 60% of the cycle
  TEST_ASSERT_TRUE(lit < total);          // but there is still a dark gap
}

void test_firefly_dark_gap_shrinks_for_longer_periods() {
  // Longer periods spend a *smaller* fraction of the cycle dark (the extra
  // seconds become glow), and even the default 7 s stays lit past half.
  TEST_ASSERT_TRUE(pmath::fireflyFlashFrac(10.0f) > pmath::fireflyFlashFrac(6.0f));
  TEST_ASSERT_TRUE(pmath::fireflyFlashFrac(7.0f) > 0.5f);
  // At the 5 s baseline the dark gap is ~25% shorter than the old flat 55% off.
  TEST_ASSERT_FLOAT_WITHIN(0.02f, 0.59f, pmath::fireflyFlashFrac(5.0f));
}

void test_firefly_flash_has_a_single_peak_that_reaches_full() {
  // Across one flash the envelope should climb to ~1.0 (the shimmer only dips
  // slightly below full at the very peak) and return to dark.
  float peak = 0.0f;
  for (int64_t us = 0; us < 7'000'000; us += 5'000) {
    peak = fmaxf(peak, pmath::fireflyIntensity(us, 0.0f, 0.0f, 7.0f, 0.0f));
  }
  TEST_ASSERT_TRUE(peak > 0.85f);
}

void test_firefly_attack_is_faster_than_decay() {
  // The flash peaks at u=0.28 of the lit window (node 0, scatter 0). Sample two
  // points the same u-distance from that peak — one on the rise, one on the
  // fade. Because the fade is slower (it spans the wider 72% of the window), the
  // point just after the peak stays brighter than the mirror point just before.
  const float period = 7.0f;
  const float flash_us = 0.45f * period * 1e6f;  // lit window length
  const float peak_u = 0.28f, d = 0.10f;
  float before = pmath::fireflyIntensity((int64_t)((peak_u - d) * flash_us), 0.0f, 0.0f, period, 0.0f);
  float after = pmath::fireflyIntensity((int64_t)((peak_u + d) * flash_us), 0.0f, 0.0f, period, 0.0f);
  TEST_ASSERT_TRUE(before > 0.0f && after > 0.0f);
  TEST_ASSERT_TRUE(after > before);
}

void test_firefly_scatter_staggers_nodes_but_unison_locks_them() {
  // scatter=0: every node flashes together, so position is irrelevant.
  int64_t t = 1'000'000;
  float a = pmath::fireflyIntensity(t, 0.0f, 0.0f, 7.0f, 0.0f);
  float b = pmath::fireflyIntensity(t, 0.6f, 0.4f, 7.0f, 0.0f);
  TEST_ASSERT_FLOAT_WITHIN(1e-5, a, b);
  // scatter=1: distinct positions get distinct phase offsets, so at least one
  // pair of nodes differs at some sampled instant (the field twinkles).
  bool any_differ = false;
  for (int64_t us = 0; us < 7'000'000 && !any_differ; us += 100'000) {
    float p0 = pmath::fireflyIntensity(us, 0.10f, 0.20f, 7.0f, 1.0f);
    float p1 = pmath::fireflyIntensity(us, 0.80f, 0.70f, 7.0f, 1.0f);
    if (fabsf(p0 - p1) > 0.05f) any_differ = true;
  }
  TEST_ASSERT_TRUE(any_differ);
}

void test_firefly_stagger_in_unit_range() {
  for (float x = -2.0f; x <= 2.0f; x += 0.31f) {
    for (float y = -2.0f; y <= 2.0f; y += 0.29f) {
      float s = pmath::fireflyStagger(x, y, 1.0f);
      TEST_ASSERT_TRUE(s >= 0.0f && s < 1.0f);
    }
  }
}

// ---- Ocean wave -----------------------------------------------------------

void test_ocean_component_bounds() {
  for (int64_t us = 0; us < 20'000'000; us += 61'000)
    for (float x = 0.0f; x <= 1.0f; x += 0.19f) {
      float v = pmath::oceanComponent(us, x, 0.3f, 0.8f, 0.6f, 9.0f, 1.0f);
      TEST_ASSERT_TRUE(v >= -1.0001f && v <= 1.0001f);
    }
}

void test_ocean_component_travels_along_direction() {
  // A wavefront traveling in +x reaches a downstream node delayed by exactly
  // period * dx / wavelength — the same waveform, time-shifted.
  const float P = 9.0f, L = 1.0f, dx = 0.3f;
  int64_t delay_us = (int64_t)((double)P * dx / L * 1e6);
  for (int64_t t = 0; t < 9'000'000; t += 250'000) {
    float a = pmath::oceanComponent(t, 0.0f, 0.0f, 1.0f, 0.0f, P, L);
    float b = pmath::oceanComponent(t + delay_us, dx, 0.0f, 1.0f, 0.0f, P, L);
    TEST_ASSERT_FLOAT_WITHIN(1e-3, a, b);
  }
}

void test_ocean_intensity_in_unit_range() {
  for (int64_t us = 0; us < 20'000'000; us += 53'000)
    for (float x = 0.0f; x <= 1.0f; x += 0.17f)
      for (float y = 0.0f; y <= 1.0f; y += 0.23f) {
        float n = pmath::oceanIntensity(us, x, y, 9.0f, 1.0f, 0.7f);
        TEST_ASSERT_TRUE(n >= 0.0f && n <= 1.0f);
      }
}

void test_ocean_intensity_swells_over_time() {
  // A fixed node should rise and fall as the swell passes — real motion.
  float lo = 2.0f, hi = -1.0f;
  for (int64_t us = 0; us < 12'000'000; us += 40'000) {
    float n = pmath::oceanIntensity(us, 0.4f, 0.6f, 9.0f, 1.0f, 0.7f);
    lo = fminf(lo, n);
    hi = fmaxf(hi, n);
  }
  TEST_ASSERT_TRUE(hi - lo > 0.3f);
}

void test_ocean_intensity_varies_across_field() {
  // One instant, different positions differ: a wave, not a uniform wash.
  int64_t t = 2'000'000;
  float lo = 2.0f, hi = -1.0f;
  for (float x = 0.0f; x <= 1.0f; x += 0.1f)
    for (float y = 0.0f; y <= 1.0f; y += 0.1f) {
      float n = pmath::oceanIntensity(t, x, y, 9.0f, 0.6f, 0.7f);
      lo = fminf(lo, n);
      hi = fmaxf(hi, n);
    }
  TEST_ASSERT_TRUE(hi - lo > 0.2f);
}

void test_ocean_angle_changes_propagation() {
  // An off-axis node sees a different phase when the travel direction changes.
  int64_t t = 1'500'000;
  float a = pmath::oceanIntensity(t, 0.2f, 0.8f, 9.0f, 1.0f, 0.0f);
  float b = pmath::oceanIntensity(t, 0.2f, 0.8f, 9.0f, 1.0f, pmath::kPi / 2.0f);
  TEST_ASSERT_TRUE(fabsf(a - b) > 0.02f);
}

// ---- Roster: conductor's MAC-keyed node list --------------------------------

// Distinct MAC per index, so tests can fabricate nodes cheaply.
static void macN(uint8_t out[6], uint8_t n) {
  out[0] = 0xDE; out[1] = 0xAD; out[2] = 0xBE; out[3] = 0xEF; out[4] = 0x00;
  out[5] = n;
}

void test_roster_starts_empty() {
  Roster r;
  rosterInit(r);
  TEST_ASSERT_EQUAL_UINT8(0, r.count);
  uint8_t mac[6];
  macN(mac, 1);
  TEST_ASSERT_EQUAL_INT(-1, rosterFind(r, mac));
}

void test_roster_appends_distinct_macs() {
  Roster r;
  rosterInit(r);
  uint8_t a[6], b[6];
  macN(a, 1);
  macN(b, 2);
  TEST_ASSERT_TRUE(rosterUpsert(r, a, 1, 1, 0x11111111, 0, "0.1.0", 100));
  TEST_ASSERT_TRUE(rosterUpsert(r, b, 2, 1, 0x11111111, 0, "0.1.0", 200));
  TEST_ASSERT_EQUAL_UINT8(2, r.count);
  TEST_ASSERT_EQUAL_INT(0, rosterFind(r, a));
  TEST_ASSERT_EQUAL_INT(1, rosterFind(r, b));
}

void test_roster_dedup_updates_in_place() {
  // The same node re-registering must refresh its row, not duplicate it.
  Roster r;
  rosterInit(r);
  uint8_t a[6];
  macN(a, 1);
  rosterUpsert(r, a, 1, 1, 0x11111111, 0, "0.1.0", 100);
  rosterUpsert(r, a, 7, 2, 0x22222222, 1, "0.2.0", 500);  // same MAC, new id/fw/time
  TEST_ASSERT_EQUAL_UINT8(1, r.count);
  int i = rosterFind(r, a);
  TEST_ASSERT_EQUAL_UINT16(7, r.entries[i].id);
  TEST_ASSERT_EQUAL_UINT8(2, r.entries[i].fw);
  TEST_ASSERT_EQUAL_UINT32(0x22222222, r.entries[i].build);
  TEST_ASSERT_EQUAL_UINT8(1, r.entries[i].dirty);
  TEST_ASSERT_EQUAL_STRING("0.2.0", r.entries[i].version);
  TEST_ASSERT_EQUAL_INT64(500, r.entries[i].last_us);
}

void test_roster_overflow_drops_new_keeps_existing() {
  Roster r;
  rosterInit(r);
  for (int n = 0; n < ROSTER_MAX; n++) {
    uint8_t m[6];
    macN(m, (uint8_t)n);
    TEST_ASSERT_TRUE(rosterUpsert(r, m, (uint16_t)n, 1, 0x11111111, 0, "0.1.0", n));
  }
  TEST_ASSERT_EQUAL_UINT8(ROSTER_MAX, r.count);
  // A brand-new MAC is dropped when full (returns false); count unchanged.
  uint8_t over[6];
  macN(over, 200);
  TEST_ASSERT_FALSE(rosterUpsert(r, over, 999, 1, 0x11111111, 0, "0.1.0", 9999));
  TEST_ASSERT_EQUAL_UINT8(ROSTER_MAX, r.count);
  // But an already-known MAC still updates in place even when full.
  uint8_t known[6];
  macN(known, 3);
  TEST_ASSERT_TRUE(rosterUpsert(r, known, 42, 1, 0x33333333, 0, "0.1.0", 12345));
  TEST_ASSERT_EQUAL_UINT16(42, r.entries[rosterFind(r, known)].id);
}

void test_firmware_version_matches_proto_build_and_dirty() {
  FirmwareVersion a = {3, 0x12345678, 0, "0.1.0"};
  FirmwareVersion b = {3, 0x12345678, 0, "0.1.0"};
  FirmwareVersion dirty = {3, 0x12345678, 1, "0.1.0"};
  FirmwareVersion other_build = {3, 0x87654321, 0, "0.1.0"};
  FirmwareVersion other_proto = {4, 0x12345678, 0, "0.1.0"};
  FirmwareVersion other_version = {3, 0x12345678, 0, "0.2.0"};

  TEST_ASSERT_TRUE(firmwareSame(a, b));
  TEST_ASSERT_FALSE(firmwareSame(a, dirty));
  TEST_ASSERT_FALSE(firmwareSame(a, other_build));
  TEST_ASSERT_FALSE(firmwareSame(a, other_proto));
  TEST_ASSERT_FALSE(firmwareSame(a, other_version));
}

void test_firmware_fleet_consistency_requires_every_seen_node_to_match() {
  FirmwareVersion expected = {3, 0x12345678, 0, "0.1.0"};
  FirmwareVersion matching[2] = {{3, 0x12345678, 0, "0.1.0"}, {3, 0x12345678, 0, "0.1.0"}};
  FirmwareVersion mixed[2] = {{3, 0x12345678, 0, "0.1.0"}, {3, 0x12345678, 1, "0.1.0"}};

  TEST_ASSERT_TRUE(firmwareFleetConsistent(expected, matching, 2));
  TEST_ASSERT_FALSE(firmwareFleetConsistent(expected, mixed, 2));
}

void test_power_policy_window_handles_daytime_and_overnight_ranges() {
  TEST_ASSERT_TRUE(powerPolicyInLedWindow(19 * 60, 18 * 60, 23 * 60));
  TEST_ASSERT_FALSE(powerPolicyInLedWindow(2 * 60, 18 * 60, 23 * 60));
  TEST_ASSERT_TRUE(powerPolicyInLedWindow(23 * 60, 18 * 60, 6 * 60));
  TEST_ASSERT_TRUE(powerPolicyInLedWindow(2 * 60, 18 * 60, 6 * 60));
  TEST_ASSERT_FALSE(powerPolicyInLedWindow(12 * 60, 18 * 60, 6 * 60));
  TEST_ASSERT_TRUE(powerPolicyInLedWindow(12 * 60, 8 * 60, 8 * 60));
}

void test_power_policy_force_awake_overrides_schedule() {
  PowerPolicy p = powerPolicyDefault();
  p.flags = POWER_FLAG_SCHEDULE_ENABLED;
  p.current_min = 12 * 60;
  p.led_on_start_min = 18 * 60;
  p.led_on_end_min = 6 * 60;

  TEST_ASSERT_FALSE(powerPolicyLedsOn(p));
  p.flags |= POWER_FLAG_FORCE_AWAKE;
  TEST_ASSERT_TRUE(powerPolicyLedsOn(p));
}

void test_power_policy_force_sleep_overrides_disabled_schedule() {
  PowerPolicy p = powerPolicyDefault();

  TEST_ASSERT_TRUE(powerPolicyLedsOn(p));
  p.flags |= POWER_FLAG_FORCE_SLEEP;
  TEST_ASSERT_FALSE(powerPolicyLedsOn(p));

  p.flags |= POWER_FLAG_FORCE_AWAKE;
  TEST_ASSERT_TRUE(powerPolicyLedsOn(p));
}

void test_power_policy_force_sleep_overrides_keepalive() {
  PowerPolicy p = powerPolicyDefault();
  p.flags = POWER_FLAG_SCHEDULE_ENABLED;
  p.current_min = 12 * 60;
  p.led_on_start_min = 20 * 60;
  p.led_on_end_min = 6 * 60;

  TEST_ASSERT_FALSE(powerPolicyShouldDeepSleep(p, true));
  TEST_ASSERT_TRUE(powerPolicyKeepaliveWindow(p));

  p.flags |= POWER_FLAG_FORCE_SLEEP;
  TEST_ASSERT_TRUE(powerPolicyShouldDeepSleep(p, true));
  TEST_ASSERT_FALSE(powerPolicyKeepaliveWindow(p));
}

void test_power_policy_sanitize_clamps_runtime_intervals() {
  PowerPolicy p = {0, 2000, 2000, 1440, 1441, 123456, 0xff};

  powerPolicySanitize(p);

  TEST_ASSERT_EQUAL_UINT16(POWER_LIGHT_CHECK_MIN_S, p.light_sleep_check_s);
  TEST_ASSERT_EQUAL_UINT16(POWER_DEEP_CHECK_MAX_MIN, p.deep_sleep_check_min);
  TEST_ASSERT_EQUAL_UINT16(560, p.led_on_start_min);
  TEST_ASSERT_EQUAL_UINT16(0, p.led_on_end_min);
  TEST_ASSERT_EQUAL_UINT16(1, p.current_min);
  TEST_ASSERT_EQUAL_UINT32(123456, p.current_epoch_s);
  TEST_ASSERT_EQUAL_UINT8(POWER_FLAG_SCHEDULE_ENABLED | POWER_FLAG_FORCE_AWAKE |
                          POWER_FLAG_FORCE_SLEEP, p.flags);
}

void test_power_policy_sleep_check_aligns_to_utc_interval() {
  PowerPolicy p = powerPolicyDefault();
  p.deep_sleep_check_min = 1;

  p.current_epoch_s = 100;
  TEST_ASSERT_EQUAL_UINT32(20, powerPolicyAlignedSleepSeconds(p));

  p.current_epoch_s = 120;
  TEST_ASSERT_EQUAL_UINT32(60, powerPolicyAlignedSleepSeconds(p));

  p.deep_sleep_check_min = 15;
  p.current_epoch_s = 3600 + 42;
  TEST_ASSERT_EQUAL_UINT32(858, powerPolicyAlignedSleepSeconds(p));

  p.current_epoch_s = 0;
  TEST_ASSERT_EQUAL_UINT32(900, powerPolicyAlignedSleepSeconds(p));
}

void test_power_policy_advance_by_seconds_preserves_off_window() {
  PowerPolicy p = powerPolicyDefault();
  p.flags = POWER_FLAG_SCHEDULE_ENABLED;
  p.current_min = 12 * 60;
  p.current_epoch_s = 3600;
  p.led_on_start_min = 20 * 60;
  p.led_on_end_min = 6 * 60;

  TEST_ASSERT_FALSE(powerPolicyLedsOn(p));
  powerPolicyAdvanceBySeconds(p, 60);

  TEST_ASSERT_EQUAL_UINT16(12 * 60 + 1, p.current_min);
  TEST_ASSERT_EQUAL_UINT32(3660, p.current_epoch_s);
  TEST_ASSERT_FALSE(powerPolicyLedsOn(p));
}

void test_keepalive_sanitizes_and_pulses() {
  KeepAliveConfig c = {KEEPALIVE_FLAG_ENABLED, 0, 0, 255};
  keepAliveSanitize(c);
  TEST_ASSERT_EQUAL_UINT16(KEEPALIVE_INTERVAL_MIN_MS, c.interval_ms);
  TEST_ASSERT_EQUAL_UINT16(KEEPALIVE_PULSE_MIN_MS, c.pulse_ms);
  TEST_ASSERT_EQUAL_UINT8(KEEPALIVE_BRIGHTNESS_MAX, c.brightness);

  c = {KEEPALIVE_FLAG_ENABLED, 10000, 100, 64};
  TEST_ASSERT_TRUE(keepAlivePulseOn(c, 0));
  TEST_ASSERT_TRUE(keepAlivePulseOn(c, 99'000));
  TEST_ASSERT_FALSE(keepAlivePulseOn(c, 100'000));
  TEST_ASSERT_TRUE(keepAlivePulseOn(c, 10'000'000));
}

void test_keepalive_disabled_never_pulses() {
  KeepAliveConfig c = {0, 10000, 100, 64};
  TEST_ASSERT_FALSE(keepAlivePulseOn(c, 0));
  TEST_ASSERT_FALSE(keepAlivePulseOn(c, 10'000'000));
}

void test_ota_crc32_matches_standard_vector() {
  const uint8_t data[] = {'1','2','3','4','5','6','7','8','9'};

  TEST_ASSERT_EQUAL_HEX32(0xCBF43926, otaCrc32Update(0, data, sizeof(data)));
}

void test_ota_hex_decode_rejects_bad_or_oversized_input() {
  uint8_t out[4];
  size_t out_len = 0;

  TEST_ASSERT_TRUE(otaHexDecode("e90010ff", out, sizeof(out), out_len));
  TEST_ASSERT_EQUAL_UINT(4, out_len);
  TEST_ASSERT_EQUAL_HEX8(0xE9, out[0]);
  TEST_ASSERT_EQUAL_HEX8(0xFF, out[3]);
  TEST_ASSERT_FALSE(otaHexDecode("abc", out, sizeof(out), out_len));
  TEST_ASSERT_FALSE(otaHexDecode("zz", out, sizeof(out), out_len));
  TEST_ASSERT_FALSE(otaHexDecode("0011223344", out, sizeof(out), out_len));
}

void test_ota_chunk_decision_accepts_repeated_written_chunks() {
  TEST_ASSERT_EQUAL_UINT8(8, OTA_RADIO_SEND_COPIES);
  TEST_ASSERT_TRUE(OTA_RADIO_SEND_MAX_ATTEMPTS >= OTA_RADIO_SEND_COPIES);
  TEST_ASSERT_TRUE(OTA_RADIO_SEND_DELAY_MS >= 4);

  TEST_ASSERT_EQUAL_UINT8(OTA_CHUNK_ACCEPT,
                          otaChunkDecision(0, 1000, 0, 200));
  TEST_ASSERT_EQUAL_UINT8(OTA_CHUNK_DUPLICATE,
                          otaChunkDecision(200, 1000, 0, 200));
  TEST_ASSERT_EQUAL_UINT8(OTA_CHUNK_OFFSET_MISMATCH,
                          otaChunkDecision(200, 1000, 100, 200));
  TEST_ASSERT_EQUAL_UINT8(OTA_CHUNK_OFFSET_MISMATCH,
                          otaChunkDecision(200, 1000, 400, 200));
  TEST_ASSERT_EQUAL_UINT8(OTA_CHUNK_OVERFLOW,
                          otaChunkDecision(900, 1000, 900, 200));
}

void test_ota_expected_chunk_len_uses_full_chunks_until_tail() {
  TEST_ASSERT_EQUAL_UINT16(OTA_SERIAL_CHUNK_MAX,
                           otaExpectedChunkLen(1000, 0));
  TEST_ASSERT_EQUAL_UINT16(OTA_SERIAL_CHUNK_MAX,
                           otaExpectedChunkLen(1000, OTA_SERIAL_CHUNK_MAX));
  TEST_ASSERT_EQUAL_UINT16(1000 - (7 * OTA_SERIAL_CHUNK_MAX),
                           otaExpectedChunkLen(1000, 7 * OTA_SERIAL_CHUNK_MAX));
  TEST_ASSERT_EQUAL_UINT16(0, otaExpectedChunkLen(1000, 1000));
}

void test_ota_status_table_upserts_by_mac() {
  OtaStatusTable t;
  otaStatusInit(t);
  const uint8_t a[6] = {1, 2, 3, 4, 5, 6};
  const uint8_t b[6] = {1, 2, 3, 4, 5, 7};

  TEST_ASSERT_TRUE(otaStatusUpsert(t, a, OTA_PHASE_BEGIN, OTA_ERR_NONE, 0, 0, 10));
  TEST_ASSERT_TRUE(otaStatusUpsert(t, b, OTA_PHASE_WRITING, OTA_ERR_NONE, 200, 42, 20));
  TEST_ASSERT_TRUE(otaStatusUpsert(t, a, OTA_PHASE_COMPLETE, OTA_ERR_NONE, 400, 99, 30));

  TEST_ASSERT_EQUAL_UINT8(2, t.count);
  int ai = otaStatusFind(t, a);
  int bi = otaStatusFind(t, b);
  TEST_ASSERT_TRUE(ai >= 0);
  TEST_ASSERT_TRUE(bi >= 0);
  TEST_ASSERT_EQUAL_UINT8(OTA_PHASE_COMPLETE, t.entries[ai].phase);
  TEST_ASSERT_EQUAL_UINT32(400, t.entries[ai].offset);
  TEST_ASSERT_EQUAL_UINT8(OTA_PHASE_WRITING, t.entries[bi].phase);
  TEST_ASSERT_EQUAL_STRING("complete", otaPhaseName(OTA_PHASE_COMPLETE));
  TEST_ASSERT_EQUAL_STRING("chunk offset mismatch", otaErrorName(OTA_ERR_OFFSET_MISMATCH));
}

void test_ota_status_complete_requires_matching_fresh_complete() {
  OtaStatusTable t;
  otaStatusInit(t);
  const uint8_t a[6] = {1, 2, 3, 4, 5, 6};
  const uint8_t b[6] = {1, 2, 3, 4, 5, 7};

  TEST_ASSERT_TRUE(otaStatusUpsert(t, a, OTA_PHASE_COMPLETE, OTA_ERR_NONE, 1000, 42, 900));
  TEST_ASSERT_TRUE(otaStatusUpsert(t, b, OTA_PHASE_COMPLETE, OTA_ERR_NONE, 999, 42, 900));

  TEST_ASSERT_TRUE(otaStatusCompleteForMac(t, a, 1000, 42, 1000, 200));
  TEST_ASSERT_FALSE(otaStatusCompleteForMac(t, a, 1000, 43, 1000, 200));
  TEST_ASSERT_FALSE(otaStatusCompleteForMac(t, b, 1000, 42, 1000, 200));
  TEST_ASSERT_FALSE(otaStatusCompleteForMac(t, a, 1000, 42, 1200, 200));
}

// ---- Layout table: authoritative MAC -> (x,y) -------------------------------

void test_table_set_and_lookup() {
  LayoutTable t;
  tableInit(t);
  uint8_t a[6], b[6];
  macN(a, 1);
  macN(b, 2);
  TEST_ASSERT_TRUE(tableSet(t, a, 1.0f, 2.0f));
  TEST_ASSERT_TRUE(tableSet(t, b, -3.5f, 4.25f));
  TEST_ASSERT_EQUAL_UINT8(2, t.count);
  float x = 0, y = 0;
  TEST_ASSERT_TRUE(tableLookup(t, b, x, y));
  TEST_ASSERT_EQUAL_FLOAT(-3.5f, x);
  TEST_ASSERT_EQUAL_FLOAT(4.25f, y);
  uint8_t miss[6];
  macN(miss, 9);
  TEST_ASSERT_FALSE(tableLookup(t, miss, x, y));
}

void test_table_set_updates_in_place() {
  // Re-assigning a known MAC moves it, never duplicates (re-arranging the field).
  LayoutTable t;
  tableInit(t);
  uint8_t a[6];
  macN(a, 1);
  tableSet(t, a, 1.0f, 1.0f);
  tableSet(t, a, 9.0f, 8.0f);
  TEST_ASSERT_EQUAL_UINT8(1, t.count);
  float x = 0, y = 0;
  tableLookup(t, a, x, y);
  TEST_ASSERT_EQUAL_FLOAT(9.0f, x);
  TEST_ASSERT_EQUAL_FLOAT(8.0f, y);
}

void test_table_remove() {
  // Node replacement: dropping a MAC leaves the others intact and findable.
  LayoutTable t;
  tableInit(t);
  uint8_t a[6], b[6], c[6];
  macN(a, 1);
  macN(b, 2);
  macN(c, 3);
  tableSet(t, a, 1, 1);
  tableSet(t, b, 2, 2);
  tableSet(t, c, 3, 3);
  TEST_ASSERT_TRUE(tableRemove(t, b));
  TEST_ASSERT_EQUAL_UINT8(2, t.count);
  TEST_ASSERT_EQUAL_INT(-1, tableFind(t, b));
  float x = 0, y = 0;  // the survivors are still correct
  TEST_ASSERT_TRUE(tableLookup(t, a, x, y));
  TEST_ASSERT_EQUAL_FLOAT(1, x);
  TEST_ASSERT_TRUE(tableLookup(t, c, x, y));
  TEST_ASSERT_EQUAL_FLOAT(3, x);
  TEST_ASSERT_FALSE(tableRemove(t, b));  // already gone
}

void test_table_overflow_drops_new() {
  LayoutTable t;
  tableInit(t);
  for (int n = 0; n < TABLE_MAX; n++) {
    uint8_t m[6];
    macN(m, (uint8_t)n);
    TEST_ASSERT_TRUE(tableSet(t, m, (float)n, 0.0f));
  }
  TEST_ASSERT_EQUAL_UINT8(TABLE_MAX, t.count);
  uint8_t over[6];
  macN(over, 200);
  TEST_ASSERT_FALSE(tableSet(t, over, 1, 1));  // full + new MAC -> dropped
  TEST_ASSERT_EQUAL_UINT8(TABLE_MAX, t.count);
  // A known MAC still updates even when full.
  uint8_t known[6];
  macN(known, 5);
  TEST_ASSERT_TRUE(tableSet(t, known, 99, 99));
  float x = 0, y = 0;
  tableLookup(t, known, x, y);
  TEST_ASSERT_EQUAL_FLOAT(99, x);
}

// ---- Heartbeat: synced square wave ------------------------------------------

void test_heartbeat_square_wave() {
  const int64_t half = 500'000;
  TEST_ASSERT_TRUE(pmath::heartbeatOn(0, half));            // on at cycle start
  TEST_ASSERT_TRUE(pmath::heartbeatOn(499'999, half));      // still on
  TEST_ASSERT_FALSE(pmath::heartbeatOn(500'000, half));     // off in 2nd half
  TEST_ASSERT_FALSE(pmath::heartbeatOn(999'999, half));     // still off
  TEST_ASSERT_TRUE(pmath::heartbeatOn(1'000'000, half));    // on again next cycle
}

void test_heartbeat_agrees_across_boards_in_sync() {
  // Two boards with different boot times but the SAME synced time must blink
  // identically — that is the whole point of the visual proof.
  const int64_t half = 500'000;
  int64_t synced = 7'250'000;  // arbitrary shared synced instant
  bool boardA = pmath::heartbeatOn(synced, half);
  bool boardB = pmath::heartbeatOn(synced, half);
  TEST_ASSERT_EQUAL(boardA, boardB);
}

void test_heartbeat_handles_negative_synced_time() {
  // Floored division keeps the square wave continuous through 0 if synced time
  // briefly goes negative (no glitch at the boundary). Bins are [k*half,(k+1)*half),
  // ON when k is even.
  const int64_t half = 500'000;
  TEST_ASSERT_FALSE(pmath::heartbeatOn(-1, half));          // k=-1 (odd): off
  TEST_ASSERT_FALSE(pmath::heartbeatOn(-500'000, half));    // k=-1 (odd): off
  TEST_ASSERT_TRUE(pmath::heartbeatOn(-750'000, half));     // k=-2 (even): on
  TEST_ASSERT_TRUE(pmath::heartbeatOn(-1'000'000, half));   // k=-2 (even): on
}

// ---- GLOW steady-color hues (the warm colors the field will hold) ------------
// glow() maps params[0] (hue degrees) onto pmath::hsvToRgb. Verify the warm hues
// we broadcast for the realistic-conservative show render as warm color: red
// strongest, no blue, green between (orange) rising toward yellow.
void test_glow_warm_hues_are_warm() {
  float r, g, b;
  pmath::hsvToRgb(30.0f / 360.0f, 1.0f, 1.0f, r, g, b);  // orange
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 1.0f, r);   // red full
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.0f, b);   // no blue
  TEST_ASSERT_TRUE(g > 0.0f && g < r);       // some green => orange
  float g_orange = g;
  pmath::hsvToRgb(50.0f / 360.0f, 1.0f, 1.0f, r, g, b);  // amber/yellow
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.0f, b);   // still no blue
  TEST_ASSERT_TRUE(g > g_orange);            // yellower => more green than orange
}

// ---- Radio duty-cycle (performer power-save schedule) ------------------------

// A small config used throughout: 4s off, 600ms listen window.
static const DutyConfig DUTY = {4'000'000, 600'000};

void test_duty_starts_on_listening() {
  DutyCycle d;
  dutyInit(d, DUTY, 0);
  TEST_ASSERT_TRUE(d.radio_on);
  TEST_ASSERT_FALSE(d.ever_caught);
  // No transition before the first window elapses.
  TEST_ASSERT_EQUAL(DUTY_NONE, dutyStep(d, DUTY, 100'000));
}

// Cold boot: until the very first beacon is caught, the window is extended rather
// than slept through — a fresh node keeps listening until it locks.
void test_duty_extends_window_until_first_catch() {
  DutyCycle d;
  dutyInit(d, DUTY, 0);
  // Window (600ms) elapses with nothing caught: stay ON, do not sleep.
  TEST_ASSERT_EQUAL(DUTY_NONE, dutyStep(d, DUTY, 600'000));
  TEST_ASSERT_TRUE(d.radio_on);
  TEST_ASSERT_EQUAL(DUTY_NONE, dutyStep(d, DUTY, 1'200'000));
  TEST_ASSERT_TRUE(d.radio_on);
  TEST_ASSERT_EQUAL_UINT32(0, d.windows);  // acquisition windows aren't counted
}

// Once acquired, a completed window sleeps the radio for off_us, then wakes.
void test_duty_sleeps_after_catch_then_wakes() {
  DutyCycle d;
  dutyInit(d, DUTY, 0);
  dutyNoteBeacon(d);  // caught a beacon during the first window
  TEST_ASSERT_TRUE(d.ever_caught);
  // Window completes -> sleep.
  TEST_ASSERT_EQUAL(DUTY_SLEEP, dutyStep(d, DUTY, 600'000));
  TEST_ASSERT_FALSE(d.radio_on);
  TEST_ASSERT_EQUAL_UINT32(1, d.windows);
  TEST_ASSERT_EQUAL_UINT32(0, d.missed_windows);  // this window caught one
  // Stays asleep until off_us elapses (600ms window + 4s off = 4.6s).
  TEST_ASSERT_EQUAL(DUTY_NONE, dutyStep(d, DUTY, 4'000'000));
  TEST_ASSERT_FALSE(d.radio_on);
  // Off interval elapsed -> wake for the next listen window.
  TEST_ASSERT_EQUAL(DUTY_WAKE, dutyStep(d, DUTY, 4'600'000));
  TEST_ASSERT_TRUE(d.radio_on);
}

// A conductor going silent mid-show: acquired once, but later windows catch
// nothing. The node must keep duty-cycling (sleep anyway) and count the misses —
// it free-runs from the synced clock and re-locks when the conductor returns.
void test_duty_sleeps_even_when_window_misses_after_acquire() {
  DutyCycle d;
  dutyInit(d, DUTY, 0);
  dutyNoteBeacon(d);
  TEST_ASSERT_EQUAL(DUTY_SLEEP, dutyStep(d, DUTY, 600'000));   // window 1, caught
  TEST_ASSERT_EQUAL(DUTY_WAKE,  dutyStep(d, DUTY, 4'600'000)); // wake window 2
  // No beacon caught this window; it must still sleep (not extend) since acquired.
  TEST_ASSERT_EQUAL(DUTY_SLEEP, dutyStep(d, DUTY, 5'200'000)); // 4'600'000+600'000
  TEST_ASSERT_FALSE(d.radio_on);
  TEST_ASSERT_EQUAL_UINT32(2, d.windows);
  TEST_ASSERT_EQUAL_UINT32(1, d.missed_windows);  // window 2 missed
}

// noteBeacon while the radio is off is ignored (we can't catch with radio down).
void test_duty_note_beacon_ignored_while_off() {
  DutyCycle d;
  dutyInit(d, DUTY, 0);
  dutyNoteBeacon(d);
  dutyStep(d, DUTY, 600'000);  // -> sleep, radio off
  TEST_ASSERT_FALSE(d.radio_on);
  d.caught = false;
  dutyNoteBeacon(d);           // off: must not mark caught
  TEST_ASSERT_FALSE(d.caught);
}

// ---- Stage B nap scheduler (CPU light-sleep between work) ---------------------

// Config used throughout: 30fps frames, 5ms floor, 1s cap, 30s serial grace.
static const NapConfig NAPC = {33'333, 5'000, 1'000'000, 30'000'000};

// Baseline inputs: radio off with the next wake far away, static pattern, serial
// long quiet, heartbeat disabled. Tests override single fields from here.
static NapInputs napBase(int64_t now) {
  NapInputs in;
  in.now_us = now;
  in.synced_us = now;
  in.radio_on = false;
  in.radio_change_at_us = now + 4'000'000;
  in.pattern_static = true;
  in.last_serial_us = now - 60'000'000;
  in.heartbeat_half_us = 0;
  return in;
}

// A listen window needs RX hot the whole time — never nap while the radio is on.
void test_nap_never_while_radio_on() {
  NapInputs in = napBase(1'000'000);
  in.radio_on = true;
  TEST_ASSERT_EQUAL_INT64(0, napPlan(NAPC, in));
}

// Serial traffic within the grace window blocks naps (light sleep drops UART
// chars; a human provisioning over USB must win over power).
void test_nap_never_during_serial_grace() {
  NapInputs in = napBase(1'000'000);
  in.last_serial_us = in.now_us - 1'000'000;  // typed 1s ago, grace is 30s
  TEST_ASSERT_EQUAL_INT64(0, napPlan(NAPC, in));
  in.last_serial_us = in.now_us - 31'000'000;  // grace expired
  TEST_ASSERT_TRUE(napPlan(NAPC, in) > 0);
}

// Static pattern, nothing sooner: nap runs to the safety cap, not to the (later)
// radio wake — no math bug may sleep a node unboundedly.
void test_nap_static_hits_safety_cap() {
  NapInputs in = napBase(1'000'000);  // radio wake 4s out, cap 1s
  TEST_ASSERT_EQUAL_INT64(1'000'000, napPlan(NAPC, in));
}

// The next radio wake bounds the nap when it's sooner than the cap: the listen
// window must never be slept through.
void test_nap_ends_at_radio_wake() {
  NapInputs in = napBase(1'000'000);
  in.radio_change_at_us = in.now_us + 200'000;
  TEST_ASSERT_EQUAL_INT64(200'000, napPlan(NAPC, in));
}

// Animated f(x,y,t) must re-render at frame cadence, so naps cap at one frame.
void test_nap_animated_caps_at_frame() {
  NapInputs in = napBase(1'000'000);
  in.pattern_static = false;
  TEST_ASSERT_EQUAL_INT64(33'333, napPlan(NAPC, in));
}

// With the heartbeat enabled, naps end at the next heartbeat edge (on the synced
// clock) so the zero-wiring sync blink stays square.
void test_nap_ends_at_heartbeat_edge() {
  NapInputs in = napBase(1'000'000);
  in.heartbeat_half_us = 500'000;
  in.synced_us = 1'234'567;  // mid-phase: 234'567 into the half-period
  TEST_ASSERT_EQUAL_INT64(500'000 - 234'567, napPlan(NAPC, in));
  // Exactly ON an edge: the NEXT edge is a full half-period away, never 0.
  in.synced_us = 1'500'000;
  TEST_ASSERT_EQUAL_INT64(500'000, napPlan(NAPC, in));
}

// Synced time can be briefly negative right after boot (offset lock). The edge
// math must still yield a delta in (0, half] — floored, not truncated, division.
void test_nap_heartbeat_edge_on_negative_synced_time() {
  NapInputs in = napBase(1'000'000);
  in.heartbeat_half_us = 500'000;
  in.synced_us = -100'000;  // 400'000 into the [-500'000, 0) half-period
  TEST_ASSERT_EQUAL_INT64(100'000, napPlan(NAPC, in));
}

// Naps shorter than the floor aren't worth the sleep/wake transition.
void test_nap_skips_tiny_naps() {
  NapInputs in = napBase(1'000'000);
  in.radio_change_at_us = in.now_us + 2'000;  // wake due in 2ms, floor is 5ms
  TEST_ASSERT_EQUAL_INT64(0, napPlan(NAPC, in));
  in.radio_change_at_us = in.now_us - 1;  // transition overdue: stay awake
  TEST_ASSERT_EQUAL_INT64(0, napPlan(NAPC, in));
}

// SOLID and GLOW have no time term (LEDs latch, no re-render needed); everything
// else animates. Unknown future ids must read as animated — the safe direction.
void test_pattern_static_ids() {
  TEST_ASSERT_TRUE(patterns::patternIsStatic(patterns::SOLID));
  TEST_ASSERT_TRUE(patterns::patternIsStatic(patterns::GLOW));
  TEST_ASSERT_FALSE(patterns::patternIsStatic(patterns::PULSE));
  TEST_ASSERT_FALSE(patterns::patternIsStatic(patterns::PALETTE_DRIFT));
  TEST_ASSERT_FALSE(patterns::patternIsStatic(patterns::SWEEP));
  TEST_ASSERT_FALSE(patterns::patternIsStatic(patterns::CALIBRATION));
  TEST_ASSERT_FALSE(patterns::patternIsStatic(999));  // unknown => animated
}

void test_calibration_code_plan_matches_hamming_sequence() {
  TEST_ASSERT_EQUAL_UINT16(1, pmath::calibrationCodeValue(1, 1, 3, 3));
  TEST_ASSERT_EQUAL_UINT16(6, pmath::calibrationCodeValue(2, 1, 3, 3));
  TEST_ASSERT_EQUAL_UINT16(0, pmath::calibrationCodeValue(3, 1, 3, 3));
}

void test_calibration_bit_sequence_is_msb_first() {
  TEST_ASSERT_FALSE(pmath::calibrationBitOn(0, 1, 1000, 3, 1, 3));
  TEST_ASSERT_FALSE(pmath::calibrationBitOn(1'000'000, 1, 1000, 3, 1, 3));
  TEST_ASSERT_TRUE(pmath::calibrationBitOn(2'000'000, 1, 1000, 3, 1, 3));
  TEST_ASSERT_TRUE(pmath::calibrationBitOn(0, 2, 1000, 3, 1, 3));
  TEST_ASSERT_TRUE(pmath::calibrationBitOn(1'000'000, 2, 1000, 3, 1, 3));
  TEST_ASSERT_FALSE(pmath::calibrationBitOn(2'000'000, 2, 1000, 3, 1, 3));
}

void test_calibration_roster_msg_fits_espnow() {
  TEST_ASSERT_LESS_OR_EQUAL_UINT16(250, sizeof(RosterMsg));
  TEST_ASSERT_EQUAL_UINT8(39, ROSTER_MACS_PER_MSG);
}

void test_calibration_roster_msg_rank_lookup() {
  RosterMsg msg = {};
  msg.n = 3;
  msg.base_rank = 39;
  const uint8_t a[6] = {0x10, 0, 0, 0, 0, 1};
  const uint8_t b[6] = {0x10, 0, 0, 0, 0, 2};
  const uint8_t c[6] = {0x10, 0, 0, 0, 0, 3};
  const uint8_t missing[6] = {0x10, 0, 0, 0, 0, 4};
  memcpy(msg.macs[0], a, 6);
  memcpy(msg.macs[1], b, 6);
  memcpy(msg.macs[2], c, 6);

  TEST_ASSERT_EQUAL_UINT16(40, rosterMsgFindRank(msg, a));
  TEST_ASSERT_EQUAL_UINT16(41, rosterMsgFindRank(msg, b));
  TEST_ASSERT_EQUAL_UINT16(42, rosterMsgFindRank(msg, c));
  TEST_ASSERT_EQUAL_UINT16(0, rosterMsgFindRank(msg, missing));
}

// ---- Daytime deep-sleep detector (Lever 2) -------------------------------------

// Config used throughout: day-above wiring, day past 1800mV / night below 900mV,
// plausible band [20, 3100], 60s debounce, 5min serial grace, 60s wake-flag TTL.
static const DuskConfig DUSKC = {true,       1800,        900,
                                 20,         3100,        60'000'000,
                                 300'000'000, 60'000'000};

static const int64_t S = 1'000'000;  // one second, in µs

// A cold boot starts in night (awake) — the power-cycle-always-wakes guarantee.
void test_dusk_cold_boot_starts_night() {
  Dusk d;
  duskInit(d, /*start_day*/ false, 0);
  TEST_ASSERT_FALSE(d.day);
}

// Steady daylight flips to day only after the full debounce, not on the first
// bright sample.
void test_dusk_flips_to_day_only_after_debounce() {
  Dusk d;
  duskInit(d, false, 0);
  TEST_ASSERT_FALSE(duskOnSample(d, DUSKC, 2500, 1 * S));   // stretch starts
  TEST_ASSERT_FALSE(duskOnSample(d, DUSKC, 2500, 30 * S));  // 29s: still night
  TEST_ASSERT_FALSE(duskOnSample(d, DUSKC, 2500, 60 * S));  // 59s: still night
  TEST_ASSERT_TRUE(duskOnSample(d, DUSKC, 2500, 61 * S));   // 60s held: day
}

// A dark interruption (cloud shadow over the sensor at dawn, a tarp) resets the
// debounce stretch — the flip needs CONTINUOUS daylight.
void test_dusk_flicker_resets_debounce() {
  Dusk d;
  duskInit(d, false, 0);
  duskOnSample(d, DUSKC, 2500, 1 * S);                       // bright stretch
  duskOnSample(d, DUSKC, 2500, 30 * S);
  duskOnSample(d, DUSKC, 100, 31 * S);                       // dark blip: reset
  duskOnSample(d, DUSKC, 2500, 32 * S);                      // new stretch
  TEST_ASSERT_FALSE(duskOnSample(d, DUSKC, 2500, 91 * S));   // 59s of new: night
  TEST_ASSERT_TRUE(duskOnSample(d, DUSKC, 2500, 92 * S));    // 60s of new: day
}

// Readings between night_mv and day_mv (dawn/dusk twilight) never flip the
// state in either direction — hysteresis dead band.
void test_dusk_dead_band_holds_current_state() {
  Dusk d;
  duskInit(d, false, 0);
  for (int i = 1; i <= 200; i++)
    TEST_ASSERT_FALSE(duskOnSample(d, DUSKC, 1400, i * S));  // night holds
  duskInit(d, true, 0);
  for (int i = 1; i <= 200; i++)
    TEST_ASSERT_TRUE(duskOnSample(d, DUSKC, 1400, i * S));   // day holds too
}

// Day flips back to night after a debounced dark stretch (dusk arrives during a
// resample wake — node stays up for the show).
void test_dusk_day_flips_to_night_at_dusk() {
  Dusk d;
  duskInit(d, true, 0);
  duskOnSample(d, DUSKC, 500, 1 * S);
  TEST_ASSERT_TRUE(duskOnSample(d, DUSKC, 500, 60 * S));
  TEST_ASSERT_FALSE(duskOnSample(d, DUSKC, 500, 61 * S));
}

// Inverted wiring (PT to GND + pull-up: daylight pulls the reading DOWN).
void test_dusk_inverted_polarity() {
  DuskConfig inv = DUSKC;
  inv.day_above = false;
  inv.day_mv = 900;    // below = day
  inv.night_mv = 1800; // above = night
  Dusk d;
  duskInit(d, false, 0);
  duskOnSample(d, inv, 300, 1 * S);                     // low reading = bright
  TEST_ASSERT_TRUE(duskOnSample(d, inv, 300, 61 * S));  // flips to day
}

// FAIL AWAKE: implausible readings (floating pin, broken wire) read as night.
// A node sleeping on a sensor that then breaks must come back awake and stay.
void test_dusk_implausible_reading_is_night() {
  Dusk d;
  duskInit(d, true, 0);  // currently day (was sleeping, woke to re-sample)
  duskOnSample(d, DUSKC, 3300, 1 * S);                     // floating-high pin
  TEST_ASSERT_FALSE(duskOnSample(d, DUSKC, 3300, 61 * S)); // debounced -> night
  duskInit(d, true, 0);
  duskOnSample(d, DUSKC, 0, 1 * S);                        // shorted/floating low
  TEST_ASSERT_FALSE(duskOnSample(d, DUSKC, 0, 61 * S));
  // And a broken sensor can never PRODUCE day from night:
  duskInit(d, false, 0);
  for (int i = 1; i <= 200; i++)
    TEST_ASSERT_FALSE(duskOnSample(d, DUSKC, 3300, i * S));
}

// The sleep gate: debounced day alone is not enough — boot hold-off, serial
// grace, and the FIELD_AWAKE beacon TTL each independently block sleep.
void test_dusk_should_sleep_gates() {
  Dusk d;
  int64_t never = INT64_MIN / 2;
  duskInit(d, false, 0);
  // Night: never sleeps, regardless of every other gate being open.
  TEST_ASSERT_FALSE(duskShouldSleep(d, DUSKC, 1000 * S, 0, never, never));
  duskInit(d, true, 0);
  // Day + all gates clear: sleeps.
  TEST_ASSERT_TRUE(duskShouldSleep(d, DUSKC, 1000 * S, 0, never, never));
  // Boot hold-off not yet passed: no sleep.
  TEST_ASSERT_FALSE(duskShouldSleep(d, DUSKC, 1000 * S, 2000 * S, never, never));
  // Serial traffic 10s ago (grace 5min): no sleep.
  TEST_ASSERT_FALSE(duskShouldSleep(d, DUSKC, 1000 * S, 0, 990 * S, never));
  // Flagged beacon 30s ago (TTL 60s): no sleep — the daytime-test override.
  TEST_ASSERT_FALSE(duskShouldSleep(d, DUSKC, 1000 * S, 0, never, 970 * S));
  // Flagged beacon 61s ago: TTL expired, sleep resumes.
  TEST_ASSERT_TRUE(duskShouldSleep(d, DUSKC, 1000 * S, 0, never, 939 * S));
}

// Timer wake from daytime sleep starts in day: still-bright readings keep it
// day (quick re-sleep once the short hold-off passes), while a dark wake
// (dusk arrived) flips it to night after the debounce.
void test_dusk_timer_wake_resample_paths() {
  Dusk d;
  int64_t never = INT64_MIN / 2;
  duskInit(d, /*start_day*/ true, 0);          // RTC flag said "was day"
  duskOnSample(d, DUSKC, 2500, 1 * S);         // still bright
  TEST_ASSERT_TRUE(d.day);
  // Short hold-off (10s) passed: allowed to re-sleep immediately.
  TEST_ASSERT_TRUE(duskShouldSleep(d, DUSKC, 11 * S, 10 * S, never, never));
  duskInit(d, true, 0);
  for (int i = 1; i <= 61; i++) duskOnSample(d, DUSKC, 400, i * S);  // dark now
  TEST_ASSERT_FALSE(d.day);                    // dusk arrived: stay up, show on
}

// THE STALE-RTC-DAY TRAP (self-review finding #1): a timer wake after sunset
// starts with day=true from RTC memory, and the short timer-wake hold-off
// (10 s) expires long before the 60 s debounce can flip the state to night.
// The gate must refuse to re-sleep while the live samples disagree with the
// (stale) day state — otherwise the node re-sleeps every 15 min all night and
// the lantern misses the entire show.
void test_dusk_dark_timer_wake_blocks_resleep() {
  Dusk d;
  int64_t never = INT64_MIN / 2;
  duskInit(d, /*start_day*/ true, 0);   // woke from daytime sleep...
  duskOnSample(d, DUSKC, 400, 1 * S);   // ...but it's dark out now
  duskOnSample(d, DUSKC, 400, 10 * S);  // hold-off expiring, debounce not done
  TEST_ASSERT_TRUE(d.day);              // state is still (stale) day...
  TEST_ASSERT_FALSE(                    // ...but sleep must be blocked
      duskShouldSleep(d, DUSKC, 10 * S, 10 * S, never, never));
  for (int i = 11; i <= 62; i++) duskOnSample(d, DUSKC, 400, i * S);
  TEST_ASSERT_FALSE(d.day);             // debounce completes: night, show on
  // Contrast: a still-bright wake (samples AGREE with day) may re-sleep.
  duskInit(d, true, 0);
  duskOnSample(d, DUSKC, 2500, 1 * S);
  TEST_ASSERT_TRUE(duskShouldSleep(d, DUSKC, 11 * S, 10 * S, never, never));
}

// ---- INA228 power telemetry (powermon.h) --------------------------------------
// The conversions feed the battery-budget math directly, the plausibility gate
// keeps a broken sensor from being trusted into it, and the report scheduler
// must defer through radio-off spans (Stage-A duty-cycling keeps the radio off
// ~87% of the time) without ever bursting to catch up.

void test_power_unit_conversions() {
  // The INA228 accumulates SI (J / C); the budget is kept in Wh / mAh.
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 1.0f, powerWh(3600.0f));    // 3600 J = 1 Wh
  TEST_ASSERT_FLOAT_WITHIN(1e-4f, 11.0f, powerWh(39600.0f));  // a target night
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 1.0f, powerMah(3.6f));      // 3.6 C = 1 mAh
  TEST_ASSERT_FLOAT_WITHIN(1e-3f, 12000.0f, powerMah(43200.0f));  // 12 Ah battery
}

void test_power_avg_watts() {
  // 3600 J over an hour is 1 W — and a zero window must not divide by zero
  // (a report can land the same second the accumulators are reset).
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 1.0f, powerAvgW(3600.0f, 3600));
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 0.0f, powerAvgW(3600.0f, 0));
}

void test_power_plausible_accepts_real_readings() {
  // A realistic overnight report from the measured rig: ~0.74 W avg @ 13.4 V.
  PowerSample s = {26640.0f, 1980.0f, 13.4f, 55.0f, 36000};
  TEST_ASSERT_TRUE(powerPlausible(s));
  // Bench edge: freshly reset, everything ~zero, is still a valid reading.
  PowerSample zero = {0.0f, 0.0f, 12.8f, 0.0f, 0};
  TEST_ASSERT_TRUE(powerPlausible(zero));
  // Backwards-wired shunt on the bench: negative current/charge is real data.
  PowerSample rev = {100.0f, -80.0f, 13.0f, -55.0f, 1000};
  TEST_ASSERT_TRUE(powerPlausible(rev));
}

void test_power_plausible_rejects_nonsense() {
  PowerSample ok = {100.0f, 80.0f, 13.0f, 55.0f, 1000};
  TEST_ASSERT_TRUE(powerPlausible(ok));

  PowerSample s = ok;
  s.energy_j = NAN;
  TEST_ASSERT_FALSE(powerPlausible(s));
  s = ok; s.energy_j = INFINITY;
  TEST_ASSERT_FALSE(powerPlausible(s));
  s = ok; s.energy_j = -1.0f;             // energy accumulator can't run backwards
  TEST_ASSERT_FALSE(powerPlausible(s));
  s = ok; s.energy_j = 1e9f;              // orders past any night on this battery
  TEST_ASSERT_FALSE(powerPlausible(s));
  s = ok; s.bus_v = 120.0f;               // divider/wiring fault
  TEST_ASSERT_FALSE(powerPlausible(s));
  s = ok; s.bus_v = -0.5f;
  TEST_ASSERT_FALSE(powerPlausible(s));
  s = ok; s.current_ma = 50000.0f;        // far past the buck's limit
  TEST_ASSERT_FALSE(powerPlausible(s));
  s = ok; s.charge_c = NAN;
  TEST_ASSERT_FALSE(powerPlausible(s));
}

void test_power_plausible_flags_reboot_inflated_avg() {
  // The skipReset design means a mid-night ESP32 reboot preserves the chip's
  // accumulator while the node's elapsed anchor restarts: a whole night's
  // Joules over a few seconds of elapsed. Every raw field is in range — only
  // the derived average exposes it (26640 J / 5 s = 5328 W "average").
  PowerSample s = {26640.0f, 1980.0f, 13.4f, 55.0f, 5};
  TEST_ASSERT_FALSE(powerPlausible(s));
  // The same totals over the real 10 h window are a normal night (~0.74 W).
  s.elapsed_s = 36000;
  TEST_ASSERT_TRUE(powerPlausible(s));
  // And elapsed 0 (report landing the same second as a reset) stays valid —
  // powerAvgW guards the division and reads as 0 W.
  PowerSample fresh = {3600.0f, 300.0f, 13.4f, 55.0f, 0};
  TEST_ASSERT_TRUE(powerPlausible(fresh));
}

void test_power_sched_first_report_immediate_then_interval() {
  PowerSched ps;
  powerSchedInit(ps);
  const int64_t I = 60000000;  // 60 s
  // First sendable moment fires immediately — a link check right after boot.
  TEST_ASSERT_TRUE(powerReportDue(ps, 5 * 1000000LL, I, true));
  // Then not again until the interval elapses.
  TEST_ASSERT_FALSE(powerReportDue(ps, 6 * 1000000LL, I, true));
  TEST_ASSERT_FALSE(powerReportDue(ps, 64 * 1000000LL, I, true));
  TEST_ASSERT_TRUE(powerReportDue(ps, 65 * 1000000LL, I, true));
}

void test_power_sched_defers_while_cannot_send_no_burst() {
  PowerSched ps;
  powerSchedInit(ps);
  const int64_t I = 60000000;
  TEST_ASSERT_TRUE(powerReportDue(ps, 0, I, true));
  // Radio stays off (or no conductor peer) across THREE due intervals…
  for (int64_t t = 1; t <= 200; t += 7)
    TEST_ASSERT_FALSE(powerReportDue(ps, t * 1000000LL, I, false));
  // …then exactly ONE catch-up report at the first sendable moment, not three.
  TEST_ASSERT_TRUE(powerReportDue(ps, 201 * 1000000LL, I, true));
  TEST_ASSERT_FALSE(powerReportDue(ps, 202 * 1000000LL, I, true));
  // And the next one is a full interval later.
  TEST_ASSERT_FALSE(powerReportDue(ps, 260 * 1000000LL, I, true));
  TEST_ASSERT_TRUE(powerReportDue(ps, 261 * 1000000LL, I, true));
}

void test_power_table_upserts_by_mac() {
  PowerTable t;
  powerTableInit(t);
  uint8_t a[6] = {1, 2, 3, 4, 5, 6};
  PowerSample first = {3600.0f, 3.6f, 13.2f, 55.0f, 3600};
  PowerSample second = {7200.0f, 7.2f, 13.1f, 56.0f, 7200};

  TEST_ASSERT_TRUE(powerTableUpsert(t, a, first, 1000));
  TEST_ASSERT_EQUAL_UINT8(1, t.count);
  int i = powerTableFind(t, a);
  TEST_ASSERT_EQUAL_INT(0, i);
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 1.0f, powerWh(t.entries[i].sample.energy_j));
  TEST_ASSERT_EQUAL_INT64(1000, t.entries[i].last_us);

  TEST_ASSERT_TRUE(powerTableUpsert(t, a, second, 2000));
  TEST_ASSERT_EQUAL_UINT8(1, t.count);
  i = powerTableFind(t, a);
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 2.0f, powerWh(t.entries[i].sample.energy_j));
  TEST_ASSERT_EQUAL_INT64(2000, t.entries[i].last_us);
}

// ---- MAC text parsing (macaddr.h) ----------------------------------------------
// Gatekeeper for the conductor's `assign`/`forget` commands — a silent misparse
// would move the wrong lantern.

void test_mac_parse_valid_any_case() {
  uint8_t m[6];
  TEST_ASSERT_TRUE(parseMac("8C:94:DF:57:7F:14", m));
  const uint8_t want[6] = {0x8C, 0x94, 0xDF, 0x57, 0x7F, 0x14};
  TEST_ASSERT_EQUAL_UINT8_ARRAY(want, m, 6);
  TEST_ASSERT_TRUE(parseMac("8c:94:df:57:7f:14", m));  // lowercase, same bytes
  TEST_ASSERT_EQUAL_UINT8_ARRAY(want, m, 6);
  TEST_ASSERT_TRUE(parseMac("0:1:2:3:4:5", m));        // unpadded digits parse
  const uint8_t low[6] = {0, 1, 2, 3, 4, 5};
  TEST_ASSERT_EQUAL_UINT8_ARRAY(low, m, 6);
}

void test_mac_parse_rejects_malformed() {
  uint8_t m[6];
  TEST_ASSERT_FALSE(parseMac("", m));
  TEST_ASSERT_FALSE(parseMac("hello", m));
  TEST_ASSERT_FALSE(parseMac("8C:94:DF:57:7F", m));       // five groups
  TEST_ASSERT_FALSE(parseMac("8C-94-DF-57-7F-14", m));    // wrong separator
  TEST_ASSERT_FALSE(parseMac("GG:94:DF:57:7F:14", m));    // non-hex group
}

// Trailing input after the sixth group must reject the WHOLE token — silently
// truncating a pasted EUI-64 to its prefix would assign/forget the wrong
// lantern (sscanf alone stops at the sixth conversion and would accept these).
void test_mac_parse_rejects_trailing_garbage() {
  uint8_t m[6];
  TEST_ASSERT_FALSE(parseMac("8C:94:DF:57:7F:14:22:31", m));  // EUI-64 paste
  TEST_ASSERT_FALSE(parseMac("8C:94:DF:57:7F:14zz", m));      // junk suffix
  TEST_ASSERT_FALSE(parseMac("8C:94:DF:57:7F:14 ", m));       // trailing space
  TEST_ASSERT_TRUE(parseMac("8C:94:DF:57:7F:14", m));         // clean still parses
}

void test_mac_parse_rejects_out_of_range_group() {
  uint8_t m[6];
  TEST_ASSERT_FALSE(parseMac("1FF:94:DF:57:7F:14", m));   // group > 0xFF
  TEST_ASSERT_FALSE(parseMac("8C:94:DF:57:7F:14000", m));
}

void test_mac_format_roundtrip() {
  uint8_t in[6], out[6];
  macN(in, 42);
  char buf[18];
  TEST_ASSERT_EQUAL_STRING("DE:AD:BE:EF:00:2A", macStr(in, buf));
  TEST_ASSERT_TRUE(parseMac(buf, out));
  TEST_ASSERT_EQUAL_UINT8_ARRAY(in, out, 6);
}

// ---- Pattern boot guard (pattern_ids.h) ----------------------------------------

void test_pattern_boot_safe() {
  // A persisted SOLID (full-white bench pattern) must not survive a power-cycle.
  TEST_ASSERT_EQUAL_UINT16(patterns::SWEEP, patterns::patternBootSafe(patterns::SOLID));
  // Every real show pattern boots as itself.
  TEST_ASSERT_EQUAL_UINT16(patterns::PULSE, patterns::patternBootSafe(patterns::PULSE));
  TEST_ASSERT_EQUAL_UINT16(patterns::PALETTE_DRIFT,
                           patterns::patternBootSafe(patterns::PALETTE_DRIFT));
  TEST_ASSERT_EQUAL_UINT16(patterns::SWEEP, patterns::patternBootSafe(patterns::SWEEP));
  TEST_ASSERT_EQUAL_UINT16(patterns::GLOW, patterns::patternBootSafe(patterns::GLOW));
  // Unknown/future ids pass through — the renderer decides what they mean.
  TEST_ASSERT_EQUAL_UINT16(999, patterns::patternBootSafe(999));
}

// ---- Machine serial JSON protocol -------------------------------------------

void test_serial_json_assign_parses_mac_and_position() {
  SerialJsonCommand cmd;
  const char* error = nullptr;

  TEST_ASSERT_TRUE(serialJsonParse(
      "{\"id\":7,\"cmd\":\"assign\",\"mac\":\"8C:94:DF:57:7F:14\",\"x\":0.25,\"y\":0.75}",
      cmd, error));

  TEST_ASSERT_NULL(error);
  TEST_ASSERT_EQUAL_UINT32(7, cmd.id);
  TEST_ASSERT_EQUAL_INT(SJ_ASSIGN, cmd.kind);
  TEST_ASSERT_EQUAL_HEX8(0x8C, cmd.mac[0]);
  TEST_ASSERT_EQUAL_FLOAT(0.25f, cmd.x);
  TEST_ASSERT_EQUAL_FLOAT(0.75f, cmd.y);
}

void test_serial_json_pattern_maps_name_brightness_and_params() {
  SerialJsonCommand cmd;
  const char* error = nullptr;

  TEST_ASSERT_TRUE(serialJsonParse(
      "{\"id\":9,\"cmd\":\"pattern\",\"pattern\":\"Palette Drift\",\"brightness\":64,"
      "\"params\":{\"period\":8000,\"spatial\":125}}",
      cmd, error));

  TEST_ASSERT_EQUAL_INT(SJ_PATTERN, cmd.kind);
  TEST_ASSERT_EQUAL_UINT16(patterns::PALETTE_DRIFT, cmd.pattern_id);
  TEST_ASSERT_TRUE(cmd.has_brightness);
  TEST_ASSERT_EQUAL_UINT8(64, cmd.brightness);
  TEST_ASSERT_TRUE(cmd.has_params[0]);
  TEST_ASSERT_TRUE(cmd.has_params[1]);
  TEST_ASSERT_EQUAL_UINT16(8000, cmd.params[0]);
  TEST_ASSERT_EQUAL_UINT16(125, cmd.params[1]);
}

void test_serial_json_glow_maps_hue_and_saturation_params() {
  SerialJsonCommand cmd;
  const char* error = nullptr;

  TEST_ASSERT_TRUE(serialJsonParse(
      "{\"id\":10,\"cmd\":\"pattern\",\"pattern\":\"Glow\",\"brightness\":48,"
      "\"params\":{\"hue\":40,\"saturation\":90}}",
      cmd, error));

  TEST_ASSERT_EQUAL_UINT16(patterns::GLOW, cmd.pattern_id);
  TEST_ASSERT_EQUAL_UINT16(40, cmd.params[0]);
  TEST_ASSERT_EQUAL_UINT16(90, cmd.params[1]);
}

void test_serial_json_calibration_maps_params() {
  SerialJsonCommand cmd;
  const char* error = nullptr;

  TEST_ASSERT_TRUE(serialJsonParse(
      "{\"id\":11,\"cmd\":\"pattern\",\"pattern\":\"Calibration\",\"brightness\":96,"
      "\"params\":{\"p0\":1000,\"p1\":3,\"p2\":1,\"p3\":3}}",
      cmd, error));

  TEST_ASSERT_EQUAL_UINT16(patterns::CALIBRATION, cmd.pattern_id);
  TEST_ASSERT_EQUAL_UINT16(1000, cmd.params[0]);
  TEST_ASSERT_EQUAL_UINT16(3, cmd.params[1]);
  TEST_ASSERT_EQUAL_UINT16(1, cmd.params[2]);
  TEST_ASSERT_EQUAL_UINT16(3, cmd.params[3]);
}

void test_serial_json_power_policy_parses_runtime_sleep_controls() {
  SerialJsonCommand cmd;
  const char* error = nullptr;

  TEST_ASSERT_TRUE(serialJsonParse(
      "{\"id\":11,\"cmd\":\"power_policy\",\"light_sleep_check_s\":30,"
      "\"deep_sleep_check_min\":60,\"led_on_start_min\":1140,"
      "\"led_on_end_min\":300,\"schedule_enabled\":true,"
      "\"force_awake\":false,\"force_sleep\":true,\"current_min\":720,"
      "\"current_epoch_s\":1720123456}",
      cmd, error));

  TEST_ASSERT_NULL(error);
  TEST_ASSERT_EQUAL_INT(SJ_POWER_POLICY, cmd.kind);
  TEST_ASSERT_TRUE(cmd.has_light_sleep_check_s);
  TEST_ASSERT_EQUAL_UINT16(30, cmd.light_sleep_check_s);
  TEST_ASSERT_TRUE(cmd.has_deep_sleep_check_min);
  TEST_ASSERT_EQUAL_UINT16(60, cmd.deep_sleep_check_min);
  TEST_ASSERT_TRUE(cmd.has_led_on_start_min);
  TEST_ASSERT_EQUAL_UINT16(1140, cmd.led_on_start_min);
  TEST_ASSERT_TRUE(cmd.has_led_on_end_min);
  TEST_ASSERT_EQUAL_UINT16(300, cmd.led_on_end_min);
  TEST_ASSERT_TRUE(cmd.has_schedule_enabled);
  TEST_ASSERT_TRUE(cmd.schedule_enabled);
  TEST_ASSERT_TRUE(cmd.has_force_awake);
  TEST_ASSERT_FALSE(cmd.force_awake);
  TEST_ASSERT_TRUE(cmd.has_force_sleep);
  TEST_ASSERT_TRUE(cmd.force_sleep);
  TEST_ASSERT_TRUE(cmd.has_current_min);
  TEST_ASSERT_EQUAL_UINT16(720, cmd.current_min);
  TEST_ASSERT_TRUE(cmd.has_current_epoch_s);
  TEST_ASSERT_EQUAL_UINT32(1720123456, cmd.current_epoch_s);
}

void test_serial_json_ota_mode_parses_enabled_flag() {
  SerialJsonCommand cmd;
  const char* error = nullptr;

  TEST_ASSERT_TRUE(serialJsonParse(
      "{\"id\":12,\"cmd\":\"ota_mode\",\"enabled\":true}",
      cmd, error));

  TEST_ASSERT_NULL(error);
  TEST_ASSERT_EQUAL_INT(SJ_OTA_MODE, cmd.kind);
  TEST_ASSERT_TRUE(cmd.has_ota_enabled);
  TEST_ASSERT_TRUE(cmd.ota_enabled);
}

void test_serial_json_ota_begin_chunk_and_end_parse() {
  SerialJsonCommand cmd;
  const char* error = nullptr;

  TEST_ASSERT_TRUE(serialJsonParse(
      "{\"id\":13,\"cmd\":\"ota_begin\",\"size\":4096,\"crc32\":1234}",
      cmd, error));
  TEST_ASSERT_EQUAL_INT(SJ_OTA_BEGIN, cmd.kind);
  TEST_ASSERT_EQUAL_UINT32(4096, cmd.ota_size);
  TEST_ASSERT_EQUAL_UINT32(1234, cmd.ota_crc32);

  TEST_ASSERT_TRUE(serialJsonParse(
      "{\"id\":14,\"cmd\":\"ota_chunk\",\"offset\":160,\"data\":\"e90010ff\"}",
      cmd, error));
  TEST_ASSERT_EQUAL_INT(SJ_OTA_CHUNK, cmd.kind);
  TEST_ASSERT_EQUAL_UINT32(160, cmd.ota_offset);
  TEST_ASSERT_EQUAL_STRING("e90010ff", cmd.ota_data_hex);

  TEST_ASSERT_TRUE(serialJsonParse("{\"id\":15,\"cmd\":\"ota_end\"}", cmd, error));
  TEST_ASSERT_EQUAL_INT(SJ_OTA_END, cmd.kind);

  TEST_ASSERT_TRUE(serialJsonParse("{\"id\":16,\"cmd\":\"ota_progress\"}", cmd, error));
  TEST_ASSERT_EQUAL_INT(SJ_OTA_PROGRESS, cmd.kind);
}

void test_serial_json_keepalive_parses_settings() {
  SerialJsonCommand cmd;
  const char* error = nullptr;

  TEST_ASSERT_TRUE(serialJsonParse(
      "{\"id\":17,\"cmd\":\"keepalive\",\"enabled\":true,"
      "\"interval_ms\":10000,\"pulse_ms\":250,\"brightness\":96}",
      cmd, error));

  TEST_ASSERT_NULL(error);
  TEST_ASSERT_EQUAL_INT(SJ_KEEPALIVE, cmd.kind);
  TEST_ASSERT_TRUE(cmd.has_keepalive_enabled);
  TEST_ASSERT_TRUE(cmd.keepalive_enabled);
  TEST_ASSERT_TRUE(cmd.has_keepalive_interval_ms);
  TEST_ASSERT_EQUAL_UINT16(10000, cmd.keepalive_interval_ms);
  TEST_ASSERT_TRUE(cmd.has_keepalive_pulse_ms);
  TEST_ASSERT_EQUAL_UINT16(250, cmd.keepalive_pulse_ms);
  TEST_ASSERT_TRUE(cmd.has_keepalive_brightness);
  TEST_ASSERT_EQUAL_UINT8(96, cmd.keepalive_brightness);
}

void test_serial_json_rejects_bad_command() {
  SerialJsonCommand cmd;
  const char* error = nullptr;

  TEST_ASSERT_FALSE(serialJsonParse("{\"id\":1,\"cmd\":\"assign\",\"mac\":\"bad\"}",
                                    cmd, error));

  TEST_ASSERT_NOT_NULL(error);
}

// ---- Table wire: chunking + validation (table_wire.h) --------------------------
// The chunk math splits a 60-node table across ESP-NOW's 250-byte payloads; the
// receive-side validation is what stands between a malformed packet and a
// memcpy overrun.

void test_table_wire_len_fits_espnow() {
  // A full chunk is exactly sizeof(TableMsg) and inside the 250 B payload cap.
  TEST_ASSERT_EQUAL_size_t(sizeof(TableMsg), tableMsgWireLen(TABLE_ROWS_PER_MSG));
  TEST_ASSERT_TRUE(tableMsgWireLen(TABLE_ROWS_PER_MSG) <= 250);
  // Zero rows is just the header + counts.
  TEST_ASSERT_EQUAL_size_t(offsetof(TableMsg, rows), tableMsgWireLen(0));
}

void test_table_chunk_count() {
  TEST_ASSERT_EQUAL_UINT8(0, tableChunkCount(0));   // empty: nothing to send
  TEST_ASSERT_EQUAL_UINT8(1, tableChunkCount(1));
  TEST_ASSERT_EQUAL_UINT8(1, tableChunkCount(TABLE_ROWS_PER_MSG));
  TEST_ASSERT_EQUAL_UINT8(2, tableChunkCount(TABLE_ROWS_PER_MSG + 1));
  TEST_ASSERT_EQUAL_UINT8(2, tableChunkCount(2 * TABLE_ROWS_PER_MSG));
  TEST_ASSERT_EQUAL_UINT8(4, tableChunkCount(TABLE_MAX));  // 64 nodes -> 4 chunks
}

void test_table_chunk_build_single_chunk() {
  LayoutTable t;
  tableInit(t);
  uint8_t a[6], b[6];
  macN(a, 1);
  macN(b, 2);
  tableSet(t, a, 1.5f, -2.0f);
  tableSet(t, b, 3.0f, 4.0f);

  TableMsg m;
  size_t len = tableChunkBuild(t, 0, m);
  TEST_ASSERT_EQUAL_size_t(tableMsgWireLen(2), len);
  TEST_ASSERT_EQUAL_UINT32(BEACON_MAGIC, m.hdr.magic);
  TEST_ASSERT_EQUAL_UINT8(PROTO_VERSION, m.hdr.version);
  TEST_ASSERT_EQUAL_UINT8(MSG_TABLE, m.hdr.type);
  TEST_ASSERT_EQUAL_UINT8(0, m.chunk);
  TEST_ASSERT_EQUAL_UINT8(1, m.chunks);
  TEST_ASSERT_EQUAL_UINT8(2, m.n);
  TEST_ASSERT_EQUAL_UINT8_ARRAY(b, m.rows[1].mac, 6);
  TEST_ASSERT_EQUAL_FLOAT(3.0f, m.rows[1].x);
  TEST_ASSERT_EQUAL_FLOAT(4.0f, m.rows[1].y);
  // Out-of-range chunk: nothing to send.
  TEST_ASSERT_EQUAL_size_t(0, tableChunkBuild(t, 1, m));
  // Empty table: every chunk index is out of range.
  LayoutTable empty;
  tableInit(empty);
  TEST_ASSERT_EQUAL_size_t(0, tableChunkBuild(empty, 0, m));
}

void test_table_chunk_build_splits_across_chunks() {
  LayoutTable t;
  tableInit(t);
  const uint8_t N = TABLE_ROWS_PER_MSG + 3;  // 20 nodes -> 17 + 3
  for (uint8_t i = 0; i < N; i++) {
    uint8_t m[6];
    macN(m, i);
    tableSet(t, m, (float)i, (float)-i);
  }
  TableMsg c0, c1;
  TEST_ASSERT_EQUAL_size_t(tableMsgWireLen(TABLE_ROWS_PER_MSG),
                           tableChunkBuild(t, 0, c0));
  TEST_ASSERT_EQUAL_size_t(tableMsgWireLen(3), tableChunkBuild(t, 1, c1));
  TEST_ASSERT_EQUAL_UINT8(2, c0.chunks);
  TEST_ASSERT_EQUAL_UINT8(2, c1.chunks);
  TEST_ASSERT_EQUAL_UINT8(1, c1.chunk);
  // Chunk 1's first row is table entry 17 — the split loses and repeats nothing.
  uint8_t want[6];
  macN(want, TABLE_ROWS_PER_MSG);
  TEST_ASSERT_EQUAL_UINT8_ARRAY(want, c1.rows[0].mac, 6);
  TEST_ASSERT_EQUAL_FLOAT((float)TABLE_ROWS_PER_MSG, c1.rows[0].x);
}

void test_table_msg_len_validation() {
  const int hdr_len = (int)offsetof(TableMsg, rows);
  // Step 1 (before the copy): raw length inside struct bounds.
  TEST_ASSERT_FALSE(tableMsgLenPlausible(0));
  TEST_ASSERT_FALSE(tableMsgLenPlausible(hdr_len - 1));
  TEST_ASSERT_TRUE(tableMsgLenPlausible(hdr_len));
  TEST_ASSERT_TRUE(tableMsgLenPlausible((int)sizeof(TableMsg)));
  TEST_ASSERT_FALSE(tableMsgLenPlausible((int)sizeof(TableMsg) + 1));
  // Step 2 (after the copy): declared row count must match the length exactly.
  TEST_ASSERT_TRUE(tableMsgLenValid((int)tableMsgWireLen(2), 2));
  TEST_ASSERT_FALSE(tableMsgLenValid((int)tableMsgWireLen(2) - 1, 2));  // truncated
  TEST_ASSERT_FALSE(tableMsgLenValid((int)tableMsgWireLen(2) + 1, 2));  // padded
  TEST_ASSERT_FALSE(tableMsgLenValid((int)tableMsgWireLen(3), 2));      // n lies low
  // n that would overrun rows[] is rejected no matter the length.
  TEST_ASSERT_FALSE(tableMsgLenValid((int)sizeof(TableMsg), TABLE_ROWS_PER_MSG + 1));
}

void test_table_msg_find_row() {
  LayoutTable t;
  tableInit(t);
  uint8_t a[6], b[6], absent[6];
  macN(a, 1);
  macN(b, 2);
  macN(absent, 99);
  tableSet(t, a, 1.0f, 2.0f);
  tableSet(t, b, -7.5f, 0.25f);
  TableMsg m;
  tableChunkBuild(t, 0, m);

  float x = 0, y = 0;
  TEST_ASSERT_TRUE(tableMsgFindRow(m, b, x, y));   // "find our own row"
  TEST_ASSERT_EQUAL_FLOAT(-7.5f, x);
  TEST_ASSERT_EQUAL_FLOAT(0.25f, y);
  TEST_ASSERT_FALSE(tableMsgFindRow(m, absent, x, y));
  // Defensive clamp: a lying n can't walk the scan past rows[].
  m.n = 200;
  TEST_ASSERT_FALSE(tableMsgFindRow(m, absent, x, y));
}

// ---- Targeted row reply (table_wire.h) -------------------------------------
// A REGISTER is the one moment the conductor knows the sender's radio is on,
// so a node that needs its position gets a single-row reply right then —
// deterministic delivery instead of the 60 s broadcast lottery through the
// ~13% radio duty cycle.

void test_table_row_reply_wanted() {
  // First join since conductor boot (or a full roster dropped the insert —
  // known-ness is computed BEFORE the upsert, so full can't mask new).
  TEST_ASSERT_TRUE(tableRowReplyWanted(/*mac_known*/ false, /*id*/ 7));
  // Unprovisioned (id 0): fresh flash / erase_flash recovery — its NVS
  // position cache is gone even though the conductor has seen the MAC.
  TEST_ASSERT_TRUE(tableRowReplyWanted(true, 0));
  // Known + provisioned re-register (every 10 s, all night): no reply —
  // steady state costs zero table traffic.
  TEST_ASSERT_FALSE(tableRowReplyWanted(true, 7));
}

void test_table_row_build() {
  LayoutTable t;
  tableInit(t);
  uint8_t a[6], b[6], absent[6];
  macN(a, 1);
  macN(b, 2);
  macN(absent, 99);
  tableSet(t, a, 1.0f, 2.0f);
  tableSet(t, b, -7.5f, 0.25f);

  TableMsg m;
  size_t len = tableRowBuild(t, b, m);
  TEST_ASSERT_EQUAL_size_t(tableMsgWireLen(1), len);  // 23 B on the wire
  TEST_ASSERT_EQUAL_UINT32(BEACON_MAGIC, m.hdr.magic);
  TEST_ASSERT_EQUAL_UINT8(PROTO_VERSION, m.hdr.version);
  TEST_ASSERT_EQUAL_UINT8(MSG_TABLE, m.hdr.type);
  TEST_ASSERT_EQUAL_UINT8(1, m.n);
  TEST_ASSERT_EQUAL_UINT8_ARRAY(b, m.rows[0].mac, 6);
  TEST_ASSERT_EQUAL_FLOAT(-7.5f, m.rows[0].x);
  TEST_ASSERT_EQUAL_FLOAT(0.25f, m.rows[0].y);
  // The receiver-side path accepts it end to end: length gates + own-row scan.
  TEST_ASSERT_TRUE(tableMsgLenPlausible((int)len));
  TEST_ASSERT_TRUE(tableMsgLenValid((int)len, m.n));
  float x = 0, y = 0;
  TEST_ASSERT_TRUE(tableMsgFindRow(m, b, x, y));
  TEST_ASSERT_EQUAL_FLOAT(-7.5f, x);
  // No row assigned yet: nothing to say (assign broadcasts when there is).
  TEST_ASSERT_EQUAL_size_t(0, tableRowBuild(t, absent, m));
}

// ---- Boot classification (bootplan.h) --------------------------------------------
// Timer wake = dusk resample rendezvous (quick re-sleep, no serial grace);
// everything else = a human (awake, long hold-off, full provisioning grace).

// Realistic config: 10 s timer min-awake, 10 min cold, 5 min dusk grace, 30 s nap.
static const BootPlanConfig BOOTC = {10 * S, 600 * S, 300 * S, 30 * S};

void test_boot_cold_boot_is_awake_and_provisionable() {
  const int64_t boot = 1000 * S;
  // rtc_was_day=true simulates stale RTC garbage surviving a flash/brownout —
  // a non-timer boot must ignore it AND clear it.
  BootPlan p = bootClassify(/*timer_wake*/ false, /*rtc_was_day*/ true, boot, BOOTC);
  TEST_ASSERT_FALSE(p.dusk_start_day);              // starts night: awake
  TEST_ASSERT_FALSE(p.rtc_day_flag);                // stale flag cleared
  TEST_ASSERT_EQUAL_INT64(boot + 600 * S, p.dusk_earliest_us);  // 10 min hold-off
  // Serial seed = boot: both the nap grace and the dusk grace start ACTIVE, so
  // a fresh flash has a responsive provisioning window and diag output.
  TEST_ASSERT_EQUAL_INT64(boot, p.serial_seed_us);
}

void test_boot_timer_wake_resamples_quickly() {
  const int64_t boot = 1000 * S;
  BootPlan p = bootClassify(/*timer_wake*/ true, /*rtc_was_day*/ true, boot, BOOTC);
  TEST_ASSERT_TRUE(p.dusk_start_day);               // resume the day state
  TEST_ASSERT_TRUE(p.rtc_day_flag);                 // flag survives for the next wake
  TEST_ASSERT_EQUAL_INT64(boot + 10 * S, p.dusk_earliest_us);  // short min-awake
  // Serial seed pre-expires BOTH graces — nothing is typing at a node that
  // woke itself in a field, and a lingering grace would block the re-sleep.
  TEST_ASSERT_TRUE(boot - p.serial_seed_us > BOOTC.dusk_serial_grace_us);
  TEST_ASSERT_TRUE(boot - p.serial_seed_us > BOOTC.nap_serial_grace_us);
}

void test_boot_timer_wake_without_day_flag_fails_awake() {
  // A timer wake whose RTC day flag reads false (corrupt RTC memory, future
  // code path) must fail toward awake: start in night like a human boot.
  BootPlan p = bootClassify(true, false, 1000 * S, BOOTC);
  TEST_ASSERT_FALSE(p.dusk_start_day);
  TEST_ASSERT_FALSE(p.rtc_day_flag);
}

void test_boot_serial_seed_expires_longest_grace() {
  // The old inline code subtracted only the dusk grace, silently relying on it
  // being the longer one. Flip the config (nap grace longer) and the seed must
  // still clear both — the invariant is gone, not just labeled.
  BootPlanConfig flipped = {10 * S, 600 * S, /*dusk*/ 30 * S, /*nap*/ 300 * S};
  const int64_t boot = 1000 * S;
  BootPlan p = bootClassify(true, true, boot, flipped);
  TEST_ASSERT_TRUE(boot - p.serial_seed_us > flipped.dusk_serial_grace_us);
  TEST_ASSERT_TRUE(boot - p.serial_seed_us > flipped.nap_serial_grace_us);
}

// ---- Runner ------------------------------------------------------------------

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_starts_unlocked);
  RUN_TEST(test_offset_reproduces_conductor_clock);
  RUN_TEST(test_first_fix_snaps_exactly);
  RUN_TEST(test_small_correction_applies_in_full);
  RUN_TEST(test_large_correction_slews_not_steps);
  RUN_TEST(test_delayed_beacon_is_gated_out);
  RUN_TEST(test_gated_beacon_still_counts_as_delivered);
  RUN_TEST(test_persistent_offset_shift_forces_relock);
  RUN_TEST(test_free_run_keeps_advancing_without_beacons);
  RUN_TEST(test_staleness_boundary);
  RUN_TEST(test_beacon_age);
  RUN_TEST(test_in_sequence_has_no_gaps);
  RUN_TEST(test_dropped_beacon_counts_one_gap);
  RUN_TEST(test_first_beacon_is_never_a_gap);
  RUN_TEST(test_seq_gap_handles_uint32_wrap);
  RUN_TEST(test_phase_range_and_wrap);
  RUN_TEST(test_phase_handles_large_time_no_overflow);
  RUN_TEST(test_pulse_intensity_bounds_and_endpoints);
  RUN_TEST(test_pulse_continuous_across_wrap);
  RUN_TEST(test_sweep_bounds);
  RUN_TEST(test_sweep_travels_with_position);
  RUN_TEST(test_sweep_nodes_differ_in_phase);
  RUN_TEST(test_hsv_primary_hues);
  RUN_TEST(test_hsv_red_to_yellow_passes_through_orange);
  RUN_TEST(test_hsv_wraps_and_stays_in_gamut);
  RUN_TEST(test_color_value_metadata_round_trips_without_losing_zero);
  RUN_TEST(test_srgb_gamma_and_rgbw_extraction_preserve_hex_distinctions);
  RUN_TEST(test_drift_hue_cycles_in_range);
  RUN_TEST(test_drift_hue_unison_by_default_but_travels_with_spatial);
  RUN_TEST(test_firefly_intensity_stays_in_gamut);
  RUN_TEST(test_firefly_lit_most_of_cycle);
  RUN_TEST(test_firefly_dark_gap_shrinks_for_longer_periods);
  RUN_TEST(test_firefly_flash_has_a_single_peak_that_reaches_full);
  RUN_TEST(test_firefly_attack_is_faster_than_decay);
  RUN_TEST(test_firefly_scatter_staggers_nodes_but_unison_locks_them);
  RUN_TEST(test_firefly_stagger_in_unit_range);
  RUN_TEST(test_ocean_component_bounds);
  RUN_TEST(test_ocean_component_travels_along_direction);
  RUN_TEST(test_ocean_intensity_in_unit_range);
  RUN_TEST(test_ocean_intensity_swells_over_time);
  RUN_TEST(test_ocean_intensity_varies_across_field);
  RUN_TEST(test_ocean_angle_changes_propagation);
  RUN_TEST(test_roster_starts_empty);
  RUN_TEST(test_roster_appends_distinct_macs);
  RUN_TEST(test_roster_dedup_updates_in_place);
  RUN_TEST(test_roster_overflow_drops_new_keeps_existing);
  RUN_TEST(test_firmware_version_matches_proto_build_and_dirty);
  RUN_TEST(test_firmware_fleet_consistency_requires_every_seen_node_to_match);
  RUN_TEST(test_power_policy_window_handles_daytime_and_overnight_ranges);
  RUN_TEST(test_power_policy_force_awake_overrides_schedule);
  RUN_TEST(test_power_policy_force_sleep_overrides_disabled_schedule);
  RUN_TEST(test_power_policy_force_sleep_overrides_keepalive);
  RUN_TEST(test_power_policy_sanitize_clamps_runtime_intervals);
  RUN_TEST(test_power_policy_sleep_check_aligns_to_utc_interval);
  RUN_TEST(test_power_policy_advance_by_seconds_preserves_off_window);
  RUN_TEST(test_keepalive_sanitizes_and_pulses);
  RUN_TEST(test_keepalive_disabled_never_pulses);
  RUN_TEST(test_ota_crc32_matches_standard_vector);
  RUN_TEST(test_ota_hex_decode_rejects_bad_or_oversized_input);
  RUN_TEST(test_ota_chunk_decision_accepts_repeated_written_chunks);
  RUN_TEST(test_ota_expected_chunk_len_uses_full_chunks_until_tail);
  RUN_TEST(test_ota_status_table_upserts_by_mac);
  RUN_TEST(test_ota_status_complete_requires_matching_fresh_complete);
  RUN_TEST(test_table_set_and_lookup);
  RUN_TEST(test_table_set_updates_in_place);
  RUN_TEST(test_table_remove);
  RUN_TEST(test_table_overflow_drops_new);
  RUN_TEST(test_heartbeat_square_wave);
  RUN_TEST(test_heartbeat_agrees_across_boards_in_sync);
  RUN_TEST(test_heartbeat_handles_negative_synced_time);
  RUN_TEST(test_glow_warm_hues_are_warm);
  RUN_TEST(test_duty_starts_on_listening);
  RUN_TEST(test_duty_extends_window_until_first_catch);
  RUN_TEST(test_duty_sleeps_after_catch_then_wakes);
  RUN_TEST(test_duty_sleeps_even_when_window_misses_after_acquire);
  RUN_TEST(test_duty_note_beacon_ignored_while_off);
  RUN_TEST(test_nap_never_while_radio_on);
  RUN_TEST(test_nap_never_during_serial_grace);
  RUN_TEST(test_nap_static_hits_safety_cap);
  RUN_TEST(test_nap_ends_at_radio_wake);
  RUN_TEST(test_nap_animated_caps_at_frame);
  RUN_TEST(test_nap_ends_at_heartbeat_edge);
  RUN_TEST(test_nap_heartbeat_edge_on_negative_synced_time);
  RUN_TEST(test_nap_skips_tiny_naps);
  RUN_TEST(test_pattern_static_ids);
  RUN_TEST(test_calibration_code_plan_matches_hamming_sequence);
  RUN_TEST(test_calibration_bit_sequence_is_msb_first);
  RUN_TEST(test_calibration_roster_msg_fits_espnow);
  RUN_TEST(test_calibration_roster_msg_rank_lookup);
  RUN_TEST(test_dusk_cold_boot_starts_night);
  RUN_TEST(test_dusk_flips_to_day_only_after_debounce);
  RUN_TEST(test_dusk_flicker_resets_debounce);
  RUN_TEST(test_dusk_dead_band_holds_current_state);
  RUN_TEST(test_dusk_day_flips_to_night_at_dusk);
  RUN_TEST(test_dusk_inverted_polarity);
  RUN_TEST(test_dusk_implausible_reading_is_night);
  RUN_TEST(test_dusk_should_sleep_gates);
  RUN_TEST(test_dusk_timer_wake_resample_paths);
  RUN_TEST(test_dusk_dark_timer_wake_blocks_resleep);
  RUN_TEST(test_power_unit_conversions);
  RUN_TEST(test_power_avg_watts);
  RUN_TEST(test_power_plausible_accepts_real_readings);
  RUN_TEST(test_power_plausible_rejects_nonsense);
  RUN_TEST(test_power_plausible_flags_reboot_inflated_avg);
  RUN_TEST(test_power_sched_first_report_immediate_then_interval);
  RUN_TEST(test_power_sched_defers_while_cannot_send_no_burst);
  RUN_TEST(test_power_table_upserts_by_mac);
  RUN_TEST(test_mac_parse_valid_any_case);
  RUN_TEST(test_mac_parse_rejects_malformed);
  RUN_TEST(test_mac_parse_rejects_trailing_garbage);
  RUN_TEST(test_mac_parse_rejects_out_of_range_group);
  RUN_TEST(test_mac_format_roundtrip);
  RUN_TEST(test_pattern_boot_safe);
  RUN_TEST(test_serial_json_assign_parses_mac_and_position);
  RUN_TEST(test_serial_json_pattern_maps_name_brightness_and_params);
  RUN_TEST(test_serial_json_glow_maps_hue_and_saturation_params);
  RUN_TEST(test_serial_json_calibration_maps_params);
  RUN_TEST(test_serial_json_power_policy_parses_runtime_sleep_controls);
  RUN_TEST(test_serial_json_ota_mode_parses_enabled_flag);
  RUN_TEST(test_serial_json_ota_begin_chunk_and_end_parse);
  RUN_TEST(test_serial_json_keepalive_parses_settings);
  RUN_TEST(test_serial_json_rejects_bad_command);
  RUN_TEST(test_table_wire_len_fits_espnow);
  RUN_TEST(test_table_chunk_count);
  RUN_TEST(test_table_chunk_build_single_chunk);
  RUN_TEST(test_table_chunk_build_splits_across_chunks);
  RUN_TEST(test_table_msg_len_validation);
  RUN_TEST(test_table_msg_find_row);
  RUN_TEST(test_table_row_reply_wanted);
  RUN_TEST(test_table_row_build);
  RUN_TEST(test_boot_cold_boot_is_awake_and_provisionable);
  RUN_TEST(test_boot_timer_wake_resamples_quickly);
  RUN_TEST(test_boot_timer_wake_without_day_flag_fails_awake);
  RUN_TEST(test_boot_serial_seed_expires_longest_grace);
  return UNITY_END();
}
