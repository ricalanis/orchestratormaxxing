/** Tailwind build config — the templates are the full class universe
 * (inline JS included; the scanner regexes class tokens out of the whole
 * file, so classes inside JS template literals are caught too).
 * Regenerate static/tailwind.css with bin/build-tailwind after UI edits. */
module.exports = {
  content: ["./templates/**/*.html", "./static/today-deal-pipeline.js"],
  theme: { extend: {} },
  corePlugins: { preflight: true },
};
