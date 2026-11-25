"use strict";

import { initRepositories } from "./ui/repos.js";
import { createAuth } from "./auth.js";
import { initChat } from "./ui/chat.js";
import { renderWorkspaceState } from "./ui/workspace.js";
import { createRouter } from "./router.js";
import { initEvaluations } from "./ui/evaluations.js";

const question = document.querySelector("#question");
const mobileQuestion = document.querySelector("#mobile-question");
const notice = document.querySelector(".composer-notice");
const mobileNotice = document.querySelector(".mobile-composer-notice");
const mobileViewport = window.matchMedia("(max-width: 900px)");
function updateMobileLayout() {
  const shellHidden = document.querySelector("#app-shell").hidden;
  const publicPage = !document.querySelector("#public-pages").hidden;
  document.querySelector(".skip-link").href = publicPage ? "#public-content" : shellHidden ? "#auth-title" : mobileViewport.matches ? "#mobile-workspace" : "#workspace";
}
mobileViewport.addEventListener("change", updateMobileLayout);
updateMobileLayout();

const chat = initChat();
const repositories = initRepositories({ autoLoad: false, onChange(state) {
  renderWorkspaceState(state);
  chat.setRepository(state, { preserveActive: ["loading", "error", "redirecting"].includes(document.documentElement.dataset.authState) });
} });
const auth = createAuth();
let workspaceActive = false;
const evaluations = initEvaluations(document.querySelector("[data-evaluations]"));
const router = createRouter({ onView(view) {
  document.querySelector("#app-shell").hidden = view !== "workspace";
  document.querySelector("#auth-screen").hidden = view !== "landing";
  document.querySelector("#public-pages").hidden = !["evaluations", "documentation"].includes(view);
  document.querySelector("#evaluations-page").hidden = view !== "evaluations";
  document.querySelector("#documentation-page").hidden = view !== "documentation";
  document.querySelectorAll("nav a[data-route]").forEach((link) => {
    if (link.getAttribute("href") === `/${view}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (view === "evaluations") evaluations.load();
  updateMobileLayout();
} });
auth.subscribe(({ phase, message, login }) => {
  const ready = phase === "signed-in" || phase === "local";
  document.documentElement.dataset.authState = phase;
  document.querySelector("[data-auth-message]").textContent = message;
  document.querySelector("[data-sign-in]").disabled = !ready && phase !== "signed-out";
  document.querySelector("[data-sign-in]").textContent = ready ? "Open workspace" : phase === "redirecting" ? "Opening GitHub…" : "Continue with GitHub";
  document.querySelector("[data-auth-retry]").hidden = phase !== "error";
  document.querySelectorAll("[data-user-login]").forEach((node) => { node.textContent = login; });
  document.querySelectorAll("[data-account]").forEach((node) => { node.hidden = phase === "local"; });
  router.setPhase(phase);
  if (phase === "signed-out") chat.clear();
  if (ready && !workspaceActive) {
    workspaceActive = true;
    repositories.start();
  } else if (!ready && workspaceActive) {
    workspaceActive = false;
    repositories.stop();
    question.value = "";
    mobileQuestion.value = "";
    document.querySelectorAll("[data-character-count]").forEach((node) => { node.textContent = "0 / 1000 characters"; });
    notice.textContent = "";
    mobileNotice.textContent = "";
    document.querySelector("[data-sign-in]").focus();
  }
});
document.querySelector("[data-sign-in]").addEventListener("click", () => {
  if (workspaceActive) router.navigate("/workspace");
  else auth.signIn();
});
document.querySelector("[data-auth-retry]").addEventListener("click", auth.initialize);
document.querySelectorAll("[data-sign-out]").forEach((button) => button.addEventListener("click", auth.signOut));
window.addEventListener("pageshow", (event) => {
  auth.resetSignIn();
  if (event.persisted) auth.initialize();
});
auth.initialize();
