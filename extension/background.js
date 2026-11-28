// The toolbar button opens the side panel. Nothing else runs in the background.
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
