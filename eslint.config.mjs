import eslint from "@eslint/js";
import globals from "globals";

export default [
    {
        files: ["**/*.js"],
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: "script",
            globals: {
                ...globals.browser,
                $: "readonly",
                ko: "readonly",
                OCTOPRINT_VIEWMODELS: "readonly",
                OctoPrint: "readonly",
                PNotify: "readonly",
                oe_do_load: "writable",
            },
        },
        rules: {
            ...eslint.configs.recommended.rules,
            "no-empty": ["error", { allowEmptyCatch: true }],
            "no-unused-vars": ["error", { args: "none" }],
        },
    },
];
