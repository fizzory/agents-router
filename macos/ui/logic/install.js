// Fixed dialog actions. No command strings or filesystem paths enter IPC.
// Titles, details and button labels are frontend copy; status.message and the
// error detail come from Rust and are shown verbatim.
export function installDialog(status = {}) {
  const version = status.version || 'this version';
  const foreign = status.foreign;
  switch (status.dialog) {
    case 'progress': return { title: status.message || 'Installing agt…', detail: '', buttons: [], busy: true };
    case 'move': return { title: 'Move Agents Router to Applications?', detail: 'It has to run from Applications to install agt.',
      buttons: [['Quit', 'quit'], ['Move to Applications', 'move']], default: 'move', cancel: 'quit' };
    case 'replace': return { title: `Replace agt ${foreign?.version || 'unknown'} from ${foreign?.source || 'another installer'}?`,
      detail: `Agents Router installs agt ${version}. Restore previous agt in General switches back.`,
      buttons: [['Keep', 'keep'], ['Replace', 'replace']], default: 'replace', cancel: 'keep' };
    case 'downgrade': return { title: 'Install an older agt?',
      detail: `agt ${version} is installed. This app has ${status.bundle_version || 'an older version'}.`,
      buttons: [['Cancel', 'cancel'], ['Downgrade', 'downgrade']], default: 'cancel', cancel: 'cancel' };
    case 'ready': return { title: 'agt is installed.', detail: 'Finish setup to connect your CLIs.',
      buttons: [['Later', 'later'], ['Set up in Terminal', 'setup']], default: 'setup', cancel: 'later' };
    case 'uninstall': return { title: 'Uninstall agt?', detail: 'Removes the agt this app installed. Your settings stay.',
      buttons: [['Cancel', 'cancel'], ['Uninstall', 'confirm-uninstall']], default: 'cancel', cancel: 'cancel' };
    case 'error': {
      const message = status.message || "Couldn't install agt.";
      const first = message.startsWith("Couldn't install agt.");
      const title = first ? "Couldn't install agt." : "Install didn't finish. Your previous agt still works.";
      return { title, detail: message.startsWith(title) ? message.slice(title.length).trim() : message,
        buttons: [['Close', 'close'], ['Retry', 'retry']], default: 'retry', cancel: 'close' };
    }
    default: return null;
  }
}
export function dialogKey(model, key) { return key === 'Escape' ? model.cancel : key === 'Enter' ? model.default : null; }
