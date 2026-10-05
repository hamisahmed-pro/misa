// Misa VS Code extension — client side.
//
// Spawns `misa lsp` (the Misa language server, impl/misa/lsp.py) and
// wires it up via vscode-languageclient. Also contributes commands
// that shell out to the `misa` CLI for check / run / build.
//
// The `misa` executable must be on PATH, or set `misa.executablePath`
// to impl/bin/misa in settings.

const vscode = require('vscode');
const { LanguageClient, TransportKind } = require('vscode-languageclient/node');

let client = null;

function misaExecutable() {
    const cfg = vscode.workspace.getConfiguration('misa');
    return cfg.get('executablePath', 'misa') || 'misa';
}

function activeMisaFile() {
    const doc = vscode.window.activeTextEditor?.document;
    if (doc && doc.languageId === 'misa') {
        return doc.fileName;
    }
    vscode.window.showWarningMessage('Misa: no active .misa file.');
    return null;
}

function runInTerminal(title, cmd) {
    const term = vscode.window.createTerminal({ name: title });
    term.show();
    term.sendText(cmd);
}

function startClient(context) {
    const exe = misaExecutable();
    const serverOptions = {
        command: exe,
        args: ['lsp'],
        transport: TransportKind.stdio,
    };
    const clientOptions = {
        documentSelector: [{ scheme: 'file', language: 'misa' }],
        synchronize: {
            // notify the server about .misa file changes on disk
            fileEvents: vscode.workspace.createFileSystemWatcher('**/*.misa'),
        },
    };
    client = new LanguageClient('misa', 'Misa Language Server',
                                serverOptions, clientOptions);
    client.start();
    context.subscriptions.push({ dispose: () => client?.stop() });
}

function activate(context) {
    startClient(context);

    const cmds = [
        ['misa.check', (f) => runInTerminal('Misa check', `${misaExecutable()} check "${f}"`)],
        ['misa.run', (f) => runInTerminal('Misa run', `${misaExecutable()} run "${f}"`)],
        ['misa.build', (f) => runInTerminal('Misa build', `${misaExecutable()} build "${f}"`)],
        ['misa.format', () => vscode.commands.executeCommand('editor.action.formatDocument')],
        ['misa.restartServer', async () => {
            await client?.stop();
            startClient(context);
            vscode.window.showInformationMessage('Misa language server restarted.');
        }],
    ];
    for (const [id, fn] of cmds) {
        context.subscriptions.push(vscode.commands.registerCommand(id, () => {
            if (id === 'misa.format' || id === 'misa.restartServer') return fn();
            const f = activeMisaFile();
            if (f) fn(f);
        }));
    }
}

function deactivate() {
    return client?.stop();
}

module.exports = { activate, deactivate };
