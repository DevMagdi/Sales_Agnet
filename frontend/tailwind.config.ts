import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        navy: "#0b1b33",
        gold: "#b8892b",
        ink: "#111827",
        muted: "#5b6779",
        canvas: "#f7f8fb",
      },
      fontFamily: {
        sans: ["system-ui", "Segoe UI", "Tahoma", "Arial", "sans-serif"],
      },
    },
  },
  plugins: [],
};

export default config;
