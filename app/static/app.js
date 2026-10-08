// navigator.clipboard only exists on https/localhost; fall back for plain LAN http.
function copyText(id, btn) {
  const text = document.getElementById(id).textContent.trim();
  const done = () => { btn.textContent = "Copied"; setTimeout(() => (btn.textContent = "Copy"), 1500); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done);
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } finally { ta.remove(); }
}
