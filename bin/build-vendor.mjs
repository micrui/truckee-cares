// Bundles the age-encryption library (typage) into one browser file.
// Output is committed so the site builds with Python alone and loads no CDN.
import { build } from "esbuild";
await build({
  entryPoints: ["bin/vendor-entry.mjs"],
  bundle: true,
  format: "esm",
  minify: true,
  target: ["es2022"],
  outfile: "site/static/js/age.js",
  banner: { js: "/* age-encryption (typage) bundled by bin/build-vendor.mjs. Do not edit. */" },
});
console.log("wrote site/static/js/age.js");
