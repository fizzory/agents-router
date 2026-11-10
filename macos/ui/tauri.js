// The only window.__TAURI__ boundary. No caller can supply a program or argv.
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
export const getSnapshot = () => invoke("get_snapshot");
export const refreshUsage = () => invoke("refresh_usage");
export const hidePopover = () => invoke("hide_popover");
export const setPopoverHeight = ({ height, durationMs, curve }) => invoke("set_popover_height", { height, durationMs, curve });
export const openSettings = ({ runtime = null, account = null } = {}) => invoke("open_settings", { runtime, account });
export const loginAccount = (runtime, account) => invoke("login_account", { runtime, account });
export const checkAccount = (runtime, account) => invoke("check_account", { runtime, account });
export const checkForUpdates = () => invoke("check_for_updates");
export const quitApp = () => invoke("quit_app");
export const onUsageUpdated = (handler) => listen("usage-updated", ({ payload }) => handler(payload));
export const onPopoverStatus = (handler) => listen("popover-status", ({ payload }) => handler(payload));
export const onUpdateRequested = (handler) => listen("update-requested", handler);
