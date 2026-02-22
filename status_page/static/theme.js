// Shared theme toggle with smooth transition
var _themeStyle = null;
function _setFavicon(theme) {
    var link = document.getElementById('favicon');
    if (!link) return;
    var accent = theme === 'dark' ? '%234080cf' : '%2376AD2A';
    link.href = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><path d='M32 6L4 20l28 14 28-14Z' fill='%23E04343'/><path d='M4 26l28 14 28-14' fill='none' stroke='%23E86235' stroke-width='4.5' stroke-linejoin='round'/><path d='M4 36l28 14 28-14' fill='none' stroke='%23FAA72A' stroke-width='4.5' stroke-linejoin='round'/><path d='M4 46l28 14 28-14' fill='none' stroke='" + accent + "' stroke-width='4.5' stroke-linejoin='round'/></svg>";
}

function toggleTheme() {
    if (!_themeStyle) {
        _themeStyle = document.createElement('style');
        _themeStyle.textContent = '*, *::before, *::after { transition: color 0.3s ease-out, background-color 0.3s ease-out, border-color 0.3s ease-out !important; }';
        document.head.appendChild(_themeStyle);
    }
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    var nextTheme = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', nextTheme);
    localStorage.setItem('theme', nextTheme);
    _setFavicon(nextTheme);
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
    var theme = 'light';
    if (saved) {
        theme = saved;
        document.documentElement.setAttribute('data-theme', theme);
    } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        theme = 'dark';
        document.documentElement.setAttribute('data-theme', theme);
    }
    _setFavicon(theme);
})();

document.addEventListener('click', function(e) {
    var btn = e.target.closest('.theme-toggle');
    if (!btn) return;
    e.preventDefault();
    toggleTheme();
});
