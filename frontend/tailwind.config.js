/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // J&J Brand Colors - Core Palette (70% of design)
        "jj-red": "#eb1700",
        "jj-white": "#ffffff",
        "jj-black": "#000000",

        // J&J Gray Scale - Neutral Palette
        "jj-gray-01": "#f1efed",
        "jj-gray-02": "#e8e6e3",
        "jj-gray-03": "#d5cfc9",
        "jj-gray-04": "#cbc4bc",
        "jj-gray-05": "#a39992",
        "jj-gray-06": "#81766f",
        "jj-gray-07": "#6e6259",
        "jj-gray-08": "#312c2a",

        // J&J Accent Colors - Limited Use (Max 30% combined)
        // Use ONLY for data visualization, illustrations, photography
        // NEVER for backgrounds or text
        "jj-yellow-01": "#fbe058",
        "jj-orange-03": "#ff6017",
        "jj-maroon-05": "#9e0000",

        // J&J Violet Scale
        "jj-violet-01": "#b19beb",
        "jj-violet-03": "#8c3bbb",
        "jj-violet-05": "#541980",

        // J&J Blue Scale
        "jj-blue-01": "#69d0ff",
        "jj-blue-03": "#0f68b2",
        "jj-blue-05": "#004685",

        // J&J Green Scale
        "jj-green-01": "#53ce76",
        "jj-green-03": "#328714",
        "jj-green-05": "#3b5a0d",
      },
      fontFamily: {
        'johnson-text': ['JohnsonText', 'Arial', 'sans-serif'],
        'johnson-display': ['JohnsonDisplay', 'Arial', 'sans-serif'],
      },
    }
  },
  plugins: [],
}