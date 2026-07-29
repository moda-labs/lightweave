"use strict";

const form = document.querySelector("#login-form");
const passwordInput = document.querySelector("#password");
const submitButton = document.querySelector("#submit-button");
const status = document.querySelector("#login-status");

function showStatus(message, kind = "error") {
  status.textContent = message;
  status.dataset.kind = kind;
}

function clearStatus() {
  status.textContent = "";
  delete status.dataset.kind;
}

function setSubmitting(submitting) {
  form.setAttribute("aria-busy", String(submitting));
  passwordInput.disabled = submitting;
  submitButton.disabled = submitting;
  submitButton.textContent = submitting ? "Signing in…" : "Sign in";
}

function loginErrorMessage(httpStatus) {
  if (httpStatus === 429) {
    return "Too many sign-in attempts. Wait a few minutes and try again.";
  }
  if (httpStatus === 413) {
    return "Unable to sign in. Reload the page and try again.";
  }
  return "Unable to sign in. Check the password and try again.";
}

async function redirectIfAuthenticated() {
  try {
    const response = await fetch("/api/auth/session", {
      credentials: "same-origin",
      headers: {Accept: "application/json"},
      cache: "no-store",
    });
    if (!response.ok) {
      return;
    }

    const session = await response.json();
    if (session?.authenticated === true) {
      window.location.replace("/");
    }
  } catch {
    // A failed session check must not prevent an operator from trying to sign in.
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearStatus();
  setSubmitting(true);
  let refocusPassword = false;

  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({password: passwordInput.value}),
    });

    if (response.ok) {
      showStatus("Signed in. Opening field control…", "success");
      window.location.replace("/");
      return;
    }

    showStatus(loginErrorMessage(response.status));
    refocusPassword = true;
  } catch {
    showStatus("Unable to reach field control. Check the connection and try again.");
    refocusPassword = true;
  } finally {
    setSubmitting(false);
    if (refocusPassword) {
      passwordInput.focus();
      passwordInput.select();
    }
  }
});

redirectIfAuthenticated();
