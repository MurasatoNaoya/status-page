document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.matches("form[data-confirm]")) return;
    var prompt = form.getAttribute("data-confirm");
    if (prompt && !window.confirm(prompt)) {
        e.preventDefault();
    }
});
