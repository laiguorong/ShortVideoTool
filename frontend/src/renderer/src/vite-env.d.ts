/// <reference types="vite/client" />

/** build-time 注入（vite.renderer.config.ts 的 define 配置）的全局版本号常量
 *  来源：frontend/package.json 的 version 字段
 *  使用：AboutDialog / 底栏显示当前系统版本，替代之前多处硬编码的版本号 */
declare const __APP_VERSION__: string