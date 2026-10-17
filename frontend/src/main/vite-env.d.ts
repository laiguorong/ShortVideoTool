/// <reference types="vite/client" />

/** vite build 时通过 define 注入的编译期常量（来源：frontend/package.json → build.appId）
 *  与 renderer/src/vite-env.d.ts 的 __APP_VERSION__ 同套机制
 *  用途：main.ts 的 app.setName(__APP_ID__)，让 runtime 的 app.getPath('userData')
 *  走跟 appId 一致的 dots 路径（package.json → name 是 npm dashes 风格，
 *  跟 OS/NSIS/Updater 缓存用的 appId 不一致，需要运行时桥接）*/
declare const __APP_ID__: string