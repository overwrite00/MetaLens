const path = require('path')
const os = require('os')
const fs = require('fs')

const iconPath = path.join(__dirname, '..', 'frontend', 'public', 'icon')
const iconExists = ['.ico', '.icns', '.png'].some(ext => fs.existsSync(iconPath + ext))

// Read version from package.json — synced by the CI workflow from python/config.py
const { version } = require('./package.json')

module.exports = {
  packagerConfig: {
    name: 'MetaLens',
    productName: 'MetaLens',          // sets Name= in .desktop file (fixes Fedora display name)
    executableName: 'metalens',
    ...(iconExists ? { icon: iconPath } : {}),
    appVersion: version,              // no longer hardcoded — reads from package.json
    extraResource: [
      // Bundled Python sidecar binary (produced by PyInstaller)
      path.join(__dirname, '..', 'python', 'dist',
        os.platform() === 'win32' ? 'metalens-sidecar.exe' : 'metalens-sidecar'),
    ],
    ignore: [/node_modules/, /\.git/],
  },
  makers: [
    {
      name: '@electron-forge/maker-squirrel',
      config: {
        name: 'MetaLens',
        setupIcon: path.join(__dirname, '..', 'frontend', 'public', 'icon.ico'),
      },
    },
    { name: '@electron-forge/maker-zip', platforms: ['darwin'] },
    {
      name: '@electron-forge/maker-deb',
      config: {
        options: {
          productName:  'MetaLens',
          genericName:  'File Metadata Manager',
          description:  'Universal File Metadata Manager — read, edit, delete and compare metadata across all major file formats.',
          homepage:     'https://github.com/overwrite00/MetaLens',
          maintainer:   'Graziano Mariella',
          icon:         path.join(__dirname, '..', 'frontend', 'public', 'icon.png'),
          categories:   ['Utility', 'FileManager'],
          section:      'utils',
        },
      },
    },
    {
      name: '@electron-forge/maker-rpm',
      config: {
        options: {
          productName:  'MetaLens',
          genericName:  'File Metadata Manager',
          description:  'Universal File Metadata Manager — read, edit, delete and compare metadata across all major file formats.',
          homepage:     'https://github.com/overwrite00/MetaLens',
          license:      'MIT',
          icon:         path.join(__dirname, '..', 'frontend', 'public', 'icon.png'),
          categories:   ['Utility', 'FileManager'],
        },
      },
    },
  ],
}
