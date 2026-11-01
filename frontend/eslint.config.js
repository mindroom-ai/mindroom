import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import prettier from "eslint-config-prettier";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import { readFileSync } from "node:fs";

// ESLint does not read .gitignore, so its rules become global ignores relative to the repository root.
// A rule with a slash before its end is anchored there; any other rule matches at any depth.
const gitIgnored = readFileSync(
  new URL("../.gitignore", import.meta.url),
  "utf8",
)
  .split("\n")
  .map((line) => line.trimEnd())
  .filter((line) => line !== "" && !line.startsWith("#"))
  .map((line) => {
    const negation = line.startsWith("!") ? "!" : "";
    const rule = line.slice(negation.length);
    const anchored = rule.slice(0, -1).includes("/");
    return negation + (anchored ? rule.replace(/^\//, "") : `**/${rule}`);
  });

export default [
  { basePath: "..", ignores: gitIgnored },
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaFeatures: { jsx: true },
        ecmaVersion: "latest",
        sourceType: "module",
      },
      globals: {
        ...globals.browser,
        ...globals.es2024,
        ...globals.node,
        ...globals.vitest,
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
      "react-hooks": reactHooks,
    },
    rules: {
      ...tsPlugin.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
        },
      ],
      "react-hooks/set-state-in-effect": "off",
      // Only says React Compiler would skip a component using TanStack Table; the dashboard does not use the compiler.
      "react-hooks/incompatible-library": "off",
    },
  },
  prettier,
];
