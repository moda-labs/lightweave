# Releasing and deploying Lightweave

Lightweave uses a small pull-based GitOps workflow. GitHub stores immutable
release assets, `main` stores the reviewed production pointer, and each Pi polls
that pointer outbound. A release and a deployment are deliberately separate
actions.

```text
merge release code -> tag vX.Y.Z -> CI publishes immutable assets
                                      |
                                      v
review production.json promotion -> merge to main
                                      |
                                      v
Pi polls, verifies, deploys control plane, stages firmware
                                      |
                                      v
operator starts field OTA when the reachable cohort is ready
```

This is GitOps for the Pi software and desired firmware artifact. It is not
unattended firmware rollout: broadcasting firmware to lanterns remains a manual,
authenticated maintenance operation.

## Release contents

Every release contains one exact set of:

- the Git tag and full commit deployed to `/opt/lightweave`;
- separate user-facing control-plane and firmware notes from `RELEASES.json`;
- the canonical `field` firmware binary;
- firmware size, SHA-256, and CRC32;
- an immutable `lightweave-release.json` manifest binding all of the above.

The production channel contains only the immutable manifest URL and the SHA-256
of its exact bytes. The Pi rejects moving branch refs, short commits, unexpected
repositories, changed manifest bytes, and firmware that fails any integrity
check.

## 1. Prepare and merge a release

In the release PR:

1. Increment `VERSION` using semantic `x.y.z` form.
2. Add the newest entry first in `RELEASES.json`. Write control-plane and
   firmware changes separately; use an empty list when one side did not change.
3. Run the normal gates:

   ```bash
   pio test -e native
   python -m pytest -q control/tests
   pio run -e field
   node --check control/static/app.js
   ```

4. Merge only after CI is green.

The release history is product data. Keep it concise, operator-facing, and free
of secrets or internal-only implementation trivia.

## 2. Publish immutable assets

Tag the exact reviewed commit on `main` and push the tag:

```bash
git switch main
git pull --ff-only
test "$(cat VERSION)" = "0.4.0"
git tag --annotate v0.4.0 --message "Lightweave v0.4.0"
git push origin v0.4.0
```

The `Publish release` GitHub Actions workflow reruns the full gates, builds
`.pio/build/field/firmware.bin`, generates `lightweave-release.json`, and creates
the GitHub release. Confirm both assets exist before promotion:

```bash
gh release view v0.4.0
gh release download v0.4.0 --pattern 'lightweave-release.json' --dir /tmp/lightweave-v0.4.0
python scripts/promote_release.py --help
```

Do not replace assets on an existing release. Correct a bad release with a new
version and tag.

## 3. Promote to production

Start a new branch from the latest `main`, then generate the production-channel
change from the immutable release URL:

```bash
git switch -c promote-v0.4.0 origin/main
python scripts/promote_release.py \
  https://github.com/underminedsk/lightweave/releases/download/v0.4.0/lightweave-release.json
git diff -- deploy/channels/production.json
git add deploy/channels/production.json
git commit -m "deploy: promote v0.4.0 to production"
git push -u origin promote-v0.4.0
gh pr create --fill
```

Review the manifest URL and hash in that PR. Merging it authorizes every enrolled
Pi to deploy the release on its next poll, normally within about six minutes.

To freeze reconciliation, replace the channel with the disabled shape and merge
that change:

```json
{
  "enabled": false,
  "manifest_sha256": null,
  "manifest_url": null,
  "schema_version": 1
}
```

## 4. Observe the Pi deployment

The timer runs the stable root-owned reconciler. On the Pi:

```bash
systemctl list-timers lightweave-gitops.timer
sudo systemctl start lightweave-gitops.service
sudo journalctl -u lightweave-gitops.service -n 100 --no-pager
git -C /opt/lightweave rev-parse HEAD
curl --silent http://127.0.0.1:8000/api/health
```

The reconciler verifies the channel, manifest, repository, tag, commit, and
firmware; backs up mutable state; checks out the detached release commit;
installs the fully hash-locked Python dependency graph; restarts the control
service; and requires a health response naming the exact expected commit. A
failed health check restores the prior code, deployment record, stable
reconciler, and units automatically. Privileged release records and firmware
live under root-owned `/var/lib/lightweave-gitops`; backups are retained under
`/var/backups/lightweave` for manual data recovery.

The reconciler and manual OTA share a root-owned, service-readable cross-process
lock. If OTA is already running when the timer fires, deployment reports
`deferred` and the next timer run retries without stopping the control plane.

The Operations page's **Deployed changes** section shows two independent states:

- **Web control plane** — running version/commit, desired commit, deployment
  time, sync state, and control changes.
- **Field firmware** — conductor version/build, field consistency, staged
  desired artifact, sync state, and firmware changes.

This distinction is intentional: immediately after a successful Pi deployment,
the control-plane card can be current while the firmware card still says an OTA
is required.

## 5. Roll firmware out to the field

The promoted firmware is automatically checksum-verified and staged in the
existing OTA store. It is never broadcast automatically.

From the authenticated Operations page:

1. Confirm the release and staged firmware shown in **Deployed changes**.
2. Confirm the conductor and currently online performers are healthy.
3. Enter the OTA maintenance window and start the install.
4. Keep power stable until every member of the frozen online cohort verifies.
5. Confirm field firmware reports the promoted clean build.

Offline layout rows are deferred rather than blocking the online cohort. Run a
later maintenance OTA when additional performers return; the normal consistency
and recovery UI shows what remains.

## Rollback

An unhealthy Pi release rolls back automatically during reconciliation. To roll
back a healthy-but-undesired release, promote an older immutable manifest using
the same promotion process. Only GitOps-capable releases (`v0.4.0` and newer)
are supported rollback targets.

Firmware rollback is a normal manual OTA using the older promoted artifact. The
Pi never changes lantern firmware merely because the control-plane release was
rolled back.

Do not manually edit `/opt/lightweave`, a deployment record, or a downloaded
firmware artifact to simulate rollback. Those changes break the integrity chain
and will be replaced or rejected by reconciliation.
