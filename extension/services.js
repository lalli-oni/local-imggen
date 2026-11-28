// The free web tiers (same names as [ref_services.*] in config.toml; a plan from `gen ref plan` carries
// the config's url/title/tips, these are the fallback for a plan text pasted straight from Claude).
// selectors: CSS to find the element to highlight, then a regex on button text/aria-label.
// BEST EFFORT: the sites change their markup, so check on use and fix here. A highlight that finds nothing
// says so; it never guesses by clicking.
const SERVICES = {
  chatgpt: {
    title: "ChatGPT", url: "https://chatgpt.com/", daily: 3,
    tips: "Say 'Create an image' first. Signs: give the exact text in quotes.",
    prompt: { css: ["#prompt-textarea", "div[contenteditable='true']"] },
    attach: { css: ["button[aria-label*='Add' i]", "button[aria-label*='Attach' i]"], text: "attach|add (photos|files)|upload" },
    generate: { css: ["button[data-testid='send-button']", "button[aria-label*='Send' i]"], text: "^send" },
    download: { css: ["button[aria-label*='Download' i]"], text: "download" },
  },
  gemini: {
    title: "Gemini", url: "https://gemini.google.com/app", daily: 3,
    tips: "Choose 'Create images'. The free allowance depends on the Google account.",
    prompt: { css: ["rich-textarea .ql-editor", "div[contenteditable='true']"] },
    attach: { css: ["button[aria-label*='upload' i]", "button[aria-label*='Add' i]"], text: "upload|add files|attach" },
    generate: { css: ["button[aria-label*='Send' i]"], text: "^send|submit" },
    download: { css: ["button[aria-label*='Download' i]"], text: "download" },
  },
  designer: {
    title: "Microsoft Designer", url: "https://designer.microsoft.com/image-creator", daily: 15,
    tips: "15 free boosts a day. Text only.",
    prompt: { css: ["textarea", "div[contenteditable='true']"] },
    attach: null,
    generate: { css: ["button[type='submit']"], text: "generate|create" },
    download: { css: ["button[aria-label*='Download' i]"], text: "download" },
  },
  leonardo: {
    title: "Leonardo", url: "https://app.leonardo.ai/image-generation", daily: 10,
    tips: "Daily free tokens. Check the licence on free images.",
    prompt: { css: ["textarea", "div[contenteditable='true']"] },
    attach: { css: ["button[aria-label*='image guidance' i]", "input[type='file']"], text: "image guidance|upload|reference" },
    generate: { css: ["button[type='submit']"], text: "generate" },
    download: { css: ["button[aria-label*='Download' i]"], text: "download" },
  },
  ideogram: {
    title: "Ideogram", url: "https://ideogram.ai/", daily: 4,
    tips: "Best at readable text in the picture. Free credits are weekly.",
    prompt: { css: ["textarea", "div[contenteditable='true']"] },
    attach: { css: ["button[aria-label*='upload' i]"], text: "upload|reference|attach" },
    generate: { css: ["button[type='submit']"], text: "generate" },
    download: { css: ["button[aria-label*='Download' i]"], text: "download" },
  },
};
