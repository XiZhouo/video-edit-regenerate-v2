/**
 * fileSystemAccess.d.ts — File System Access API 的最小环境类型补丁。
 *
 * TS 的 lib.dom 已带 FileSystemDirectoryHandle / FileSystemFileHandle /
 * createWritable / FileSystemWritableFileStream,但缺少下面三处(浏览器已实现、
 * 标准 lib 尚未收录):showDirectoryPicker / showOpenFilePicker(入口)与 handle
 * 的 queryPermission / requestPermission(F5 重连所需)。本文件只声明本模块实际
 * 用到的最小子集,不引入第三方 @types。
 */

export {}

interface FileSystemHandlePermissionDescriptor {
  mode?: 'read' | 'readwrite'
}

interface DirectoryPickerOptions {
  id?: string
  mode?: 'read' | 'readwrite'
  startIn?: FileSystemHandle | string
}

interface FilePickerAcceptType {
  description?: string
  accept: Record<string, string | string[]>
}

interface OpenFilePickerOptions {
  id?: string
  multiple?: boolean
  excludeAcceptAllOption?: boolean
  types?: FilePickerAcceptType[]
  startIn?: FileSystemHandle | string
}

declare global {
  interface FileSystemHandle {
    queryPermission(descriptor?: FileSystemHandlePermissionDescriptor): Promise<PermissionState>
    requestPermission(descriptor?: FileSystemHandlePermissionDescriptor): Promise<PermissionState>
  }

  interface Window {
    showDirectoryPicker(options?: DirectoryPickerOptions): Promise<FileSystemDirectoryHandle>
    showOpenFilePicker(options?: OpenFilePickerOptions): Promise<FileSystemFileHandle[]>
  }
}
