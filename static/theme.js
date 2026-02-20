// Shared theme toggle with smooth transition
var _themeStyle = null;
function toggleTheme() {
    if (!_themeStyle) {
        _themeStyle = document.createElement('style');
        _themeStyle.textContent = '*, *::before, *::after { transition: color 0.3s ease-out, background-color 0.3s ease-out, border-color 0.3s ease-out !important; }';
        document.head.appendChild(_themeStyle);
    }
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
    localStorage.setItem('theme', isDark ? 'light' : 'dark');
    clearTimeout(toggleTheme._timer);
    toggleTheme._timer = setTimeout(function() {
        if (_themeStyle && _themeStyle.parentNode) {
            _themeStyle.parentNode.removeChild(_themeStyle);
            _themeStyle = null;
        }
    }, 400);
}
// Restore theme: use saved preference, fall back to system preference
(function() {
    var saved = localStorage.getItem('theme');
    if (saved) {
        document.documentElement.setAttribute('data-theme', saved);
    } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        document.documentElement.setAttribute('data-theme', 'dark');
    }
})();
