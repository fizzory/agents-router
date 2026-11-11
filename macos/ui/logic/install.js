// Fixed dialog actions. No command strings or filesystem paths enter IPC.
export function installDialog(status = {}) {
  const version = status.version || 'this version';
  const foreign = status.foreign;
  switch (status.dialog) {
    case 'progress': return { title: status.message || 'Installing agt…', buttons: [], busy: true };
    case 'move': return { title: 'Move Agents Router to Applications to continue.', buttons: [['Quit', 'quit'], ['Move to Applications', 'move']], default: 'move', cancel: 'quit' };
    case 'replace': return { title: `Replace agt ${foreign?.version || 'unknown'} (${foreign?.source || 'source'}) with ${version}?`, buttons: [['Keep', 'keep'], ['Replace', 'replace']], default: 'replace', cancel: 'keep' };
    case 'downgrade': return { title: `Replace agt ${version} with ${status.bundle_version || 'the older version'}?`, buttons: [['Cancel', 'cancel'], ['Downgrade', 'downgrade']], default: 'cancel', cancel: 'cancel' };
    case 'ready': return { title: 'agt is ready.', buttons: [['Later', 'later'], ['Set up', 'setup']], default: 'setup', cancel: 'later' };
    case 'uninstall': return { title: 'Uninstall command line tool?', buttons: [['Cancel', 'cancel'], ['Uninstall', 'confirm-uninstall']], default: 'confirm-uninstall', cancel: 'cancel' };
    case 'error': {
      const message = status.message || "Couldn't install agt.";
      const first = message.startsWith("Couldn't install agt.");
      const title = first ? "Couldn't install agt." : "Install didn't finish. Your previous agt still works.";
      return { title, detail: message.startsWith(title) ? message.slice(title.length).trim() : message, buttons: [['Close', 'close'], ['Try Again', 'retry']], default: 'retry', cancel: 'close' };
    }
    default: return null;
  }
}
export function dialogKey(model, key) { return key === 'Escape' ? model.cancel : key === 'Enter' ? model.default : null; }
