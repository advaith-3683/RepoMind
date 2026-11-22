let timer;
export function toast(message) {
  let region = document.querySelector("[data-toast]");
  if (!region) {
    region = document.createElement("div");
    region.dataset.toast = "";
    region.className = "toast";
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.append(region);
  }
  region.textContent = message;
  region.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => { region.hidden = true; }, 6000);
}
