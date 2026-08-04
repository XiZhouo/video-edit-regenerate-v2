/**
 * 04-user-facing/03-authentication — 账号 / 项目 / 协作的前端入口(桶导出)。
 *
 * webapp 经 `@ze/core/04-user-facing/03-authentication` 引用。这是 core 能力:
 * 任意 ZE skill 复制 01-core 即获得账号 + 多项目按人隔离 + 共享。
 * (后端对端为同目录 auth.py / identity.py;经 sdk 导出。)
 */

export { AuthProvider, useAuth } from './AuthContext'
export { ProjectProvider, useProject } from './ProjectContext'
export { LoginPage } from './LoginPage'
export { ProjectPicker } from './ProjectPicker'
export { UserPicker, type UserPickerProps } from './UserPicker'
export {
  authHeader, getToken, setToken, clearToken, triggerAuthError, apiListUsers,
  apiAuthConfig,
  apiSearchUsers, apiLookupUsers,
  apiListCredentials, apiListCursorModels, apiSaveCredential, apiVerifyCredential, apiDeleteCredential,
  apiSetDefaultCredential,
  apiCliLoginStart, apiCliLoginPoll, apiCliLoginSubmitCode, apiCliLoginCancel,
  apiGetAccountModelPolicy, apiRequestEditPermission, apiAdminListPolicies, apiAdminUpdatePolicy,
  apiGetConfig, apiGetConfigSnapshot, apiPutConfig, newAuthIdempotencyKey,
  apiGetProjectMutationStatus, apiGetDemoMutationStatus,
  apiGetProjectGitLink, apiPutProjectGitLink, apiDeleteProjectGitLink,
} from './authApi'
export type {
  AuthUser, AuthApiError, AuthMethod, AuthRuntimeConfig,
  ProjectSummary, ConfigItem, ConfigSnapshot, ZeUserCard,
  CredentialProvider, CredentialProviderInfo, CredentialRecord, CursorModelOption,
  ModelScope, AccountModelPolicy, CliLoginState, ProjectMutationStatus, ProjectTopologyIntentKind,
  DemoTemplateCatalog, AuthRequestContext,
  CreateProjectInput, DuplicateProjectInput, DeleteProjectInput, OpenDemoTemplateInput,
  ProjectGitLink,
} from './authApi'
export type {
  ProjectMutationIntent, ProjectMutationIntentState, ProjectMutationKind,
} from './projectMutationIntent'
