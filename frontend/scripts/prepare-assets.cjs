#!/usr/bin/env node
/**
 * prepare-assets.cjs
 *
 * 从 GitHub Releases 下载后端资源包（ms-playwright + ffmpeg）并解压到 backend/assets/。
 *
 * 设计目标：
 *   - CI 跑（GitHub Actions）：从 $GITHUB_REPOSITORY / $GITHUB_TOKEN 读上下文
 *   - 本地手测：可 --owner-repo / --version 覆盖
 *   - 版本来源：../backend/assets_version.txt（默认），或 --version 覆盖
 *
 * 用法:
 *   node prepare-assets.cjs
 *   node prepare-assets.cjs --owner-repo your-org/ShortVideoTool --version v1.0.0
 *   node prepare-assets.cjs --help
 *
 * 环境变量:
 *   GITHUB_REPOSITORY   Actions 自动注入，形如 owner/repo
 *   GITHUB_TOKEN        Actions 自动注入，私有 Release 鉴权用
 */

const https = require('https');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

// ---------- 参数解析 ----------
function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--owner-repo') out.ownerRepo = argv[++i];
    else if (a === '--version') out.version = argv[++i];
    else if (a === '--dest') out.dest = argv[++i];
    else if (a === '--help' || a === '-h') out.help = true;
  }
  return out;
}

function log(msg) { console.log(`[prepare-assets] ${msg}`); }
function err(msg) { console.error(`[prepare-assets] ❌ ${msg}`); }

function showHelp() {
  console.log(`
用法: node prepare-assets.cjs [选项]

选项:
  --owner-repo <owner/repo>  GitHub 仓库（默认 $GITHUB_REPOSITORY）
  --version <vX.Y.Z>        资源版本（默认读 ../backend/assets_version.txt）
  --dest <dir>              解压目标（默认 ../backend/assets）
  -h, --help                显示帮助

环境变量:
  GITHUB_REPOSITORY         GitHub Actions 自动注入，形如 owner/repo
  GITHUB_TOKEN              GitHub Actions 自动注入，私有 Release 鉴权用
`);
}

// ---------- 下载（自动跟随 GitHub Release 的 302 -> S3 重定向） ----------
function downloadWithRedirects(url, outPath, maxRedirects = 5) {
  return new Promise((resolve, reject) => {
    if (maxRedirects <= 0) return reject(new Error('Too many redirects'));
    const headers = { 'User-Agent': 'prepare-assets-script' };
    if (process.env.GITHUB_TOKEN) {
      headers['Authorization'] = `Bearer ${process.env.GITHUB_TOKEN}`;
    }
    const req = https.get(url, { headers }, (res) => {
      if (res.statusCode === 301 || res.statusCode === 302) {
        const next = res.headers.location;
        res.resume();
        if (!next) return reject(new Error(`Redirect without Location header from ${url}`));
        log(`重定向 (${res.statusCode}) -> ${next.slice(0, 80)}...`);
        downloadWithRedirects(next, outPath, maxRedirects - 1).then(resolve, reject);
        return;
      }
      if (res.statusCode === 404) {
        return reject(new Error(
          `Release 资源不存在 (404): ${url}\n` +
          `请确认 backend/assets_version.txt 里的版本已上传到 GitHub Release（tag = assets-${guessVersionFromUrl(url)}）`
        ));
      }
      if (res.statusCode !== 200) {
        return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
      }
      const totalBytes = parseInt(res.headers['content-length'] || '0', 10);
      let received = 0;
      const file = fs.createWriteStream(outPath);
      res.on('data', (chunk) => {
        received += chunk.length;
        if (totalBytes) {
          const pct = ((received / totalBytes) * 100).toFixed(1);
          process.stdout.write(
            `\r[prepare-assets] 下载进度: ${pct}% ` +
            `(${(received / 1024 / 1024).toFixed(1)}MB / ${(totalBytes / 1024 / 1024).toFixed(1)}MB)   `
          );
        }
      });
      res.pipe(file);
      file.on('finish', () => {
        file.close(() => {
          if (totalBytes) process.stdout.write('\n');
          resolve();
        });
      });
      file.on('error', (e) => {
        fs.unlink(outPath, () => reject(e));
      });
    });
    req.on('error', reject);
    req.setTimeout(120000, () => req.destroy(new Error('Download timeout (120s)')));
  });
}

// 失败信息里方便排查的辅助函数（404 时拼出版本提示）
function guessVersionFromUrl(url) {
  const m = url.match(/assets-(v[\d.]+)\.zip/);
  return m ? m[1] : '?';
}

// ---------- 主流程 ----------
async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    showHelp();
    return;
  }

  const scriptDir = __dirname;
  const defaultVersionFile = path.resolve(scriptDir, '..', '..', 'backend', 'assets_version.txt');
  const defaultDest = path.resolve(scriptDir, '..', '..', 'backend', 'assets');

  // 1. 仓库
  const ownerRepo = args.ownerRepo || process.env.GITHUB_REPOSITORY;
  if (!ownerRepo) {
    err('未指定仓库。请用 --owner-repo your-org/ShortVideoTool 或设置 GITHUB_REPOSITORY 环境变量');
    process.exit(1);
  }
  if (!/^[^/]+\/[^/]+$/.test(ownerRepo)) {
    err(`仓库格式错误: ${ownerRepo}（应为 owner/repo）`);
    process.exit(1);
  }

  // 2. 版本
  let version = args.version;
  if (!version) {
    if (!fs.existsSync(defaultVersionFile)) {
      err(`找不到版本文件: ${defaultVersionFile}`);
      err('请创建 backend/assets_version.txt 写入当前资源版本（如 v1.0.0），或用 --version 覆盖');
      process.exit(1);
    }
    version = fs.readFileSync(defaultVersionFile, 'utf8').trim();
  }
  if (!/^v\d+\.\d+\.\d+/.test(version)) {
    err(`版本格式错误: ${version}（应为 vX.Y.Z）`);
    process.exit(1);
  }

  // 3. 路径与 URL
  const dest = path.resolve(args.dest || defaultDest);
  const tag = `assets-${version}`;
  const zipName = `assets-${version}.zip`;
  const url = `https://github.com/${ownerRepo}/releases/download/${tag}/${zipName}`;
  const tmpZip = path.join(scriptDir, `_tmp_${zipName}`);

  log(`仓库: ${ownerRepo}`);
  log(`资源版本: ${version} -> tag: ${tag}`);
  log(`解压目录: ${dest}`);
  log(`下载地址: ${url}`);

  try {
    log('开始下载...');
    await downloadWithRedirects(url, tmpZip);
    const sizeMB = (fs.statSync(tmpZip).size / 1024 / 1024).toFixed(1);
    log(`下载完成 (${sizeMB} MB)`);

    fs.mkdirSync(dest, { recursive: true });
    log(`解压到 ${dest}...`);
    if (process.platform === 'win32') {
      execSync(
        `powershell -NoProfile -ExecutionPolicy Bypass -Command ` +
        `"Expand-Archive -Force -Path '${tmpZip}' -DestinationPath '${dest}'"`,
        { stdio: 'inherit' }
      );
    } else {
      execSync(`unzip -q -o "${tmpZip}" -d "${dest}"`, { stdio: 'inherit' });
    }
    fs.unlinkSync(tmpZip);
    log('✅ 完成');
  } catch (e) {
    err(e.message);
    if (fs.existsSync(tmpZip)) {
      try { fs.unlinkSync(tmpZip); } catch (_) { /* ignore */ }
    }
    process.exit(1);
  }
}

main();