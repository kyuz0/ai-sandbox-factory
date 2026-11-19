const vscode = require('vscode');

exports.activate = (context) => {
    const openFirstChat = async () => {
        if (context.workspaceState.get('frostPiOpened') || !vscode.workspace.workspaceFolders?.length) return;
        await vscode.commands.executeCommand('frostpi.newSession');
        await context.workspaceState.update('frostPiOpened', true);
    };
    if (vscode.workspace.isTrusted) return openFirstChat();
    context.subscriptions.push(vscode.workspace.onDidGrantWorkspaceTrust(openFirstChat));
};
