// The practice's sign-in: email, password and the robot check (see app/admin_auth.py). The robot check is solved
// here, in the browser: find the number whose SHA-256 hash the server asked for. It takes about a second.

const form = document.getElementById("sign-in");
const email = document.getElementById("email");
const password = document.getElementById("password");
const robot = document.getElementById("robot");
const robotStatus = document.getElementById("robot-status");
const submit = document.getElementById("submit");
const errorBox = document.getElementById("error");
const BATCH = 1000; // hashes tried at a time
let solution = null;

const showError = (text) => { errorBox.textContent = text; errorBox.classList.toggle("hidden", !text); };

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function solve() {
  const response = await fetch("/api/admin/robot-check", { cache: "no-store" });
  if (!response.ok) throw new Error("unavailable");
  const check = await response.json();
  for (let start = 0; start <= check.maxnumber; start += BATCH) {
    const numbers = Array.from({ length: Math.min(BATCH, check.maxnumber - start + 1) }, (_, i) => start + i);
    const hashes = await Promise.all(numbers.map((n) => sha256Hex(`${check.salt}${n}`)));
    const found = hashes.indexOf(check.challenge);
    if (found >= 0) {
      return { salt: check.salt, challenge: check.challenge, signature: check.signature, number: numbers[found] };
    }
  }
  throw new Error("not solved");
}

function resetRobot() {
  solution = null;
  robot.checked = false;
  robot.disabled = false;
  robotStatus.textContent = "";
  submit.disabled = true;
}

robot.addEventListener("change", async () => {
  if (!robot.checked) { resetRobot(); return; }
  if (!window.crypto?.subtle) {
    resetRobot();
    showError("This page must be opened over a secure (https) connection.");
    return;
  }
  robot.disabled = true;
  robotStatus.textContent = "Checking…";
  try {
    solution = await solve();
    robotStatus.textContent = "Verified";
    submit.disabled = false;
  } catch {
    resetRobot();
    showError("The robot check could not be completed. Please tick it again.");
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  if (!email.value.trim() || !password.value) { showError("Please enter your email and password."); return; }
  if (!solution) { showError("Please tick the robot check."); return; }
  submit.disabled = true;
  try {
    const response = await fetch("/api/admin/sign-in", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "admin" },
      body: JSON.stringify({ email: email.value.trim(), password: password.value, robot: solution }),
    });
    if (response.ok) { location.assign("/admin"); return; }
    let detail = "Sign-in failed. Please try again.";
    try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
    showError(detail);
  } catch {
    showError("Sign-in failed. Please check your connection and try again.");
  }
  password.value = "";
  resetRobot(); // each robot check works once
});
