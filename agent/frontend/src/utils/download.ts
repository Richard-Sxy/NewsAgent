/** 触发浏览器下载。导出接口返回 Blob，不能直接跳转——网关可能要求带 Cookie。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // 立即释放会让部分浏览器来不及读取，延后一拍更稳。
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** 用时间戳拼一个不会互相覆盖的文件名。 */
export function timestampedFilename(base: string, extension: string): string {
  const now = new Date()
  const pad = (value: number) => String(value).padStart(2, '0')
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(
    now.getHours(),
  )}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  const suffix = extension.startsWith('.') ? extension : `.${extension}`
  return `${base}-${stamp}${suffix}`
}
