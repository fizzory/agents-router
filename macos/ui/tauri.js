// The only window.__TAURI__ boundary. No caller can supply a program or argv.
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
export const getSnapshot = () => invoke("get_snapshot");
export const refreshUsage = () => invoke("refresh_usage");
export const hidePopover = () => invoke("hide_popover");
export const setPopoverHeight = ({ height, durationMs, curve }) => invoke("set_popover_height", { height, durationMs, curve });
export const openSettings = ({ runtime = null, account = null, install = false } = {}) => invoke("open_settings", { runtime, account, install });
export const loginAccount = (runtime, account) => invoke("login_account", { runtime, account });
export const checkAccount = (runtime, account) => invoke("check_account", { runtime, account });
export const checkForUpdates = () => invoke("check_for_updates");
export const quitApp = () => invoke("quit_app");
export const onUsageUpdated = (handler) => listen("usage-updated", ({ payload }) => handler(payload));
export const onPopoverStatus = (handler) => listen("popover-status", ({ payload }) => handler(payload));
export const onUpdateRequested = (handler) => listen("update-requested", handler);

// Settings capability only. Scope is {workspace: absolute folder or null}; writes
// additionally require {key, level, revision, userSaid?}, and set takes {value}.
export const configShow = (workspace = null) => invoke("config_show", { workspace });
export const configSet = ({ key, value, level, workspace = null, revision, userSaid = null }) =>
  invoke("config_set", { key, value, level, workspace, revision, userSaid });
export const configUnset = ({ key, level, workspace = null, revision, userSaid = null }) =>
  invoke("config_unset", { key, level, workspace, revision, userSaid });
export const configRevert = ({ entry, workspace = null, userSaid = null }) => invoke("config_revert", { entry, workspace, userSaid });
export const rolesShow = (workspace = null) => invoke("roles_show", { workspace });
export const rolesConfig = ({ edit, level, workspace = null, userSaid = null }) =>
  invoke("roles_config", { edit, level, workspace, userSaid });
export const accountsList = () => invoke("accounts_list");
export const accountsCheck = (runtime, account) => invoke("accounts_check", { runtime, account });
export const accountsAddCheck = (runtime, account) => invoke("accounts_add_check", { runtime, account });
export const accountsToggle = (runtime, account, enabled) => invoke("accounts_toggle", { runtime, account, enabled });
export const accountsLogin = (runtime, account) => invoke("accounts_login", { runtime, account });
export const accountsAdd = (runtime, account) => invoke("accounts_add", { runtime, account });
export const shareList = () => invoke("share_list");
export const shareStatus = () => invoke("share_status");
export const shareKind = (kind, enabled) => invoke("share_kind", { kind, enabled });
export const sharePick = (item, label, included) => invoke("share_pick", { item, label, included });
export const activityLog = () => invoke("activity_log");
export const openSettingsFile = (file, line = 1) => invoke("open_settings_file", { file, line });
export const chooseWorkspace = () => invoke("choose_workspace");
export const getOpenAtLogin = () => invoke("get_open_at_login");
export const setOpenAtLogin = (enabled) => invoke("set_open_at_login", { enabled });
export const getSettingsTarget = () => invoke("get_settings_target");
export const showSettingMenu = (key) => invoke("show_setting_menu", { key });
export const onSettingsHistory = (handler) => listen("settings-history", ({ payload }) => handler(payload));
export const onSettingsTarget = (handler) => listen("settings-target", ({ payload }) => handler(payload));
export const onSettingsEdit = (handler) => listen("settings-edit", ({ payload }) => handler(payload));
export const onSettingsStatus = (handler) => listen("settings-status", ({ payload }) => handler(payload));

export const showPaneMenu = (token, actions) => invoke("show_pane_menu", { token, actions });
export const onPaneMenu = handler => listen("pane-menu", ({ payload }) => handler(payload));
export const activityUndoSync = entry => invoke("activity_undo_sync", { entry });
// Native context-menu callbacks do not carry WebKit user activation.
export const copyText = text => invoke("copy_text", { text });

export const setupRoles = () => invoke("setup_roles");

export const getInstallStatus = () => invoke("get_install_status");
export const onInstallStatus = handler => listen("install-status", ({ payload }) => handler(payload));
export const installAction = action => invoke("install_action", { action });
