(function () {
  function copyToClipboard(value, button) {
    if (!value) return;
    var original = button.textContent;
    var done = function () {
      button.textContent = "کپی شد";
      window.setTimeout(function () {
        button.textContent = original;
      }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done).catch(function () {});
      return;
    }
    var textarea = document.createElement("textarea");
    textarea.value = value;
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    document.body.removeChild(textarea);
    done();
  }

  document.addEventListener("click", function (event) {
    var revealButton = event.target.closest("[data-config-link-reveal]");
    if (revealButton) {
      var target = document.getElementById(revealButton.getAttribute("data-config-link-reveal"));
      if (!target) return;
      target.textContent = revealButton.getAttribute("data-config-full-link") || "";
      target.hidden = !target.hidden ? true : false;
      revealButton.textContent = target.hidden ? "نمایش لینک کامل" : "پنهان کردن لینک کامل";
      return;
    }

    var copyButton = event.target.closest("[data-config-link-copy]");
    if (copyButton) {
      copyToClipboard(copyButton.getAttribute("data-config-link-copy") || "", copyButton);
    }
  });
})();
