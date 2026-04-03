/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["DM Sans", "system-ui", "sans-serif"],
        display: ["Syne", "system-ui", "sans-serif"],
      },
      borderRadius: {
        card: "14px",
      },
      boxShadow: {
        glass: "0 8px 32px rgba(0, 0, 0, 0.35), 0 2px 8px rgba(0, 0, 0, 0.2)",
        "glass-hover": "0 16px 48px rgba(0, 0, 0, 0.45), 0 4px 16px rgba(14, 165, 233, 0.12)",
      },
      backgroundImage: {
        "page-dark":
          "radial-gradient(ellipse 120% 80% at 50% -20%, rgba(56, 189, 248, 0.15), transparent 50%), radial-gradient(ellipse 80% 50% at 100% 50%, rgba(99, 102, 241, 0.08), transparent), linear-gradient(180deg, #050914 0%, #0a1020 40%, #060a14 100%)",
        "btn-doctor": "linear-gradient(90deg, #14b8a6 0%, #0ea5e9 55%, #2563eb 100%)",
        "btn-patient": "linear-gradient(90deg, #2563eb 0%, #3b82f6 50%, #60a5fa 100%)",
      },
    },
  },
  plugins: [],
};
