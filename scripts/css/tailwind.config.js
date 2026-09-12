// Tailwind build for the portal. The page used Tailwind's in-browser "play"
// CDN (v3.4.17), which compiles styles on every page load and is not meant
// for production; this builds the same classes ONCE into
// static/vendor/tailwind.css, served by the portal itself.
//
// Tailwind finds classes by scanning these files for complete class names, so
// never assemble one from pieces ('text-' + colour) — write the whole name.
// Rebuild after using a class the page has not used before: scripts/css/build.sh
module.exports = {
    content: [
        './src/release_agent/static/**/*.js',
        '!./src/release_agent/static/vendor/**',
        './src/release_agent/app_fastapi.py',       // the page shell lives in a Python string
    ],
    theme: { extend: {} },
    plugins: [],
};
