(function () {
    function ready(callback) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", callback);
        } else {
            callback();
        }
    }

    ready(function () {
        var messageBox = document.getElementById("id_message_text");
        var counter = document.getElementById("campaign-char-count");
        if (messageBox && counter) {
            var updateCounter = function () {
                counter.textContent = String(messageBox.value.length);
            };
            messageBox.addEventListener("input", updateCounter);
            updateCounter();
        }

        var dataNode = document.getElementById("campaign-message-templates");
        var templates = dataNode ? JSON.parse(dataNode.textContent || "[]") : [];
        var byKey = {};
        templates.forEach(function (item) {
            byKey[item.key] = item.body || "";
        });
        document.querySelectorAll("[data-campaign-template]").forEach(function (button) {
            button.addEventListener("click", function () {
                if (!messageBox || messageBox.disabled) {
                    return;
                }
                messageBox.value = byKey[button.getAttribute("data-campaign-template")] || "";
                messageBox.focus();
                messageBox.dispatchEvent(new Event("input"));
            });
        });

        document.querySelectorAll("[data-confirm-phrase]").forEach(function (form) {
            var phrase = form.getAttribute("data-confirm-phrase") || "";
            var input = form.querySelector("input[name='confirmation']");
            var button = form.querySelector("button[type='submit']");
            if (!input || !button || button.disabled) {
                return;
            }
            var updateState = function () {
                button.disabled = input.value.trim() !== phrase;
            };
            input.addEventListener("input", updateState);
            updateState();
        });
    });
})();
