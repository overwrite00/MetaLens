const path = require('path')
const os = require('os')

module.exports = {
  packagerConfig: {
    name: 'MetaLens',
    executableName: 'metalens',
    icon: path.join(__dirname, '..', 'frontend', 'public', 'icon'),
    appVersion: '0.1.0',
    extraResource: [
      // Bundled Python sidecar binary (produced by PyInstaller)
      path.join(__dirname, '..', 'python', 'dist',
        os.platform() === 'win32' ? 'metalens-sidecar.exe' : 'metalens-sidecar'),
    ],
    ignore: [/node_modules/, /\.git/],
  },
  makers: [
    { name: '@electron-forge/maker-squirrel', config: { name: 'MetaLens' } },
    { name: '@electron-forge/maker-zip',      platforms: ['darwin'] },
    { name: '@electron-forge/maker-deb',      config: {} },
    { name: '@electron-forge/maker-rpm',      config: {} },
  ],
}
