(function () {
  if (window.matchMedia("(max-width: 991.98px)").matches) {
    document.body.classList.remove("sidebar-open");
    document.body.classList.add("sidebar-collapse");
  }

  const labels = [
    ["store/order", "جستجوی سفارش"],
    ["store/customer", "جستجوی مشتری"],
    ["store/supportconversation", "جستجوی پشتیبانی"],
    ["payments/incomingpaymentsms", "جستجوی پرداخت"],
    ["auth/user", "جستجوی کاربر"],
  ];

  document.querySelectorAll("#jazzy-navbar form").forEach((form) => {
    const input = form.querySelector("input[type='search']");
    if (!input) {
      return;
    }
    const action = (form.getAttribute("action") || "").toLowerCase();
    const match = labels.find(([path]) => action.includes(path));
    const placeholder = match ? match[1] : "جستجوی سریع";
    form.classList.add("qasedak-navbar-search");
    input.setAttribute("placeholder", placeholder);
    input.setAttribute("aria-label", placeholder);
  });
})();
