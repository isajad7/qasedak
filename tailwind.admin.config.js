/** @type {import('tailwindcss').Config} */
module.exports = {
  // Tailwind v4 stores the prefix without a trailing dash; admin class names remain tw-*.
  prefix: "tw",
  corePlugins: {
    preflight: false,
  },
  content: [
    "./templates/admin/**/*.html",
    "./store/**/*.py",
  ],
  theme: {
    extend: {},
  },
  plugins: [],
};
