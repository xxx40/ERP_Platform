import {
  UserManager,
  WebStorageStateStore,
  type User,
  type UserManagerSettings,
} from "oidc-client-ts";

export type AuthenticationMode = "oidc" | "development" | "unconfigured";

export interface AuthenticationState {
  mode: AuthenticationMode;
  authenticated: boolean;
  loginRequired: boolean;
  error: string;
}

export interface DevelopmentIdentity {
  id: string;
  label: string;
  userId: string;
  tenantId: string;
  orgCode: string;
  roles: string[];
}

const authority = String(import.meta.env.VITE_OIDC_AUTHORITY ?? "").trim();
const clientId = String(import.meta.env.VITE_OIDC_CLIENT_ID ?? "").trim();
const configuredForOidc = Boolean(authority && clientId);
const developmentIdentityAllowed = import.meta.env.DEV;

export const authenticationMode: AuthenticationMode = configuredForOidc
  ? "oidc"
  : developmentIdentityAllowed
    ? "development"
    : "unconfigured";

export const developmentIdentities: DevelopmentIdentity[] = [
  {
    id: "employee",
    label: "普通员工",
    userId: "employee-user",
    tenantId: "tenant-demo",
    orgCode: "ORG-DEMO-001",
    roles: ["employee"],
  },
  {
    id: "procurement-specialist",
    label: "采购专员",
    userId: "procurement-specialist",
    tenantId: "tenant-demo",
    orgCode: "ORG-DEMO-001",
    roles: ["procurement_specialist"],
  },
  {
    id: "procurement-manager",
    label: "采购经理",
    userId: "procurement-manager",
    tenantId: "tenant-demo",
    orgCode: "ORG-DEMO-001",
    roles: ["procurement_manager"],
  },
  {
    id: "data-reviewer",
    label: "数据源审批员",
    userId: "data-reviewer",
    tenantId: "tenant-demo",
    orgCode: "ORG-DEMO-001",
    roles: ["data_source_reviewer"],
  },
  {
    id: "platform-admin",
    label: "平台管理员",
    userId: "platform-admin",
    tenantId: "tenant-demo",
    orgCode: "ORG-DEMO-001",
    roles: ["platform_admin"],
  },
];

const developmentIdentityKey = "erp-assistant-development-identity";

function configuredDevelopmentIdentity(): DevelopmentIdentity {
  return {
    id: "configured",
    label: "王主管",
    userId: import.meta.env.VITE_DEMO_USER_ID ?? "demo-user",
    tenantId: import.meta.env.VITE_DEMO_TENANT_ID ?? "tenant-demo",
    orgCode: import.meta.env.VITE_DEMO_ORG_CODE ?? "ORG-DEMO-001",
    roles: String(import.meta.env.VITE_DEMO_ROLES ?? "procurement_manager,platform_admin")
      .split(",")
      .map((role) => role.trim())
      .filter(Boolean),
  };
}

export function getDevelopmentIdentity(): DevelopmentIdentity {
  const selectedId = sessionStorage.getItem(developmentIdentityKey);
  return developmentIdentities.find((identity) => identity.id === selectedId)
    ?? configuredDevelopmentIdentity();
}

export function setDevelopmentIdentity(identityId: string): void {
  if (authenticationMode !== "development" || !import.meta.env.DEV) return;
  const identity = developmentIdentities.find((item) => item.id === identityId);
  if (!identity) throw new Error("未知的开发测试身份");
  sessionStorage.setItem(developmentIdentityKey, identity.id);
}

function redirectUri(): string {
  return `${window.location.origin}${window.location.pathname}`;
}

let manager: UserManager | null = null;

function oidcManager(): UserManager {
  if (authenticationMode !== "oidc") {
    throw new Error("OIDC 尚未配置");
  }
  if (manager) return manager;
  const configuredRedirectUri = import.meta.env.VITE_OIDC_REDIRECT_URI ?? redirectUri();
  const settings: UserManagerSettings = {
    authority,
    client_id: clientId,
    redirect_uri: configuredRedirectUri,
    silent_redirect_uri: import.meta.env.VITE_OIDC_SILENT_REDIRECT_URI ?? configuredRedirectUri,
    post_logout_redirect_uri: import.meta.env.VITE_OIDC_POST_LOGOUT_REDIRECT_URI ?? redirectUri(),
    response_type: "code",
    scope: import.meta.env.VITE_OIDC_SCOPE ?? "openid profile email offline_access",
    automaticSilentRenew: true,
    monitorSession: true,
    revokeTokensOnSignout: true,
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
  };
  manager = new UserManager(settings);
  manager.events.addAccessTokenExpired(() => {
    window.dispatchEvent(new CustomEvent("erp-authentication-required"));
  });
  manager.events.addSilentRenewError(() => {
    window.dispatchEvent(new CustomEvent("erp-authentication-required"));
  });
  return manager;
}

function isSigninCallback(): boolean {
  const parameters = new URLSearchParams(window.location.search);
  return parameters.has("state") && (parameters.has("code") || parameters.has("error"));
}

function isSignoutCallback(): boolean {
  const parameters = new URLSearchParams(window.location.search);
  return parameters.has("state") && !parameters.has("code") && !parameters.has("error");
}

function cleanCallbackUrl(): void {
  const parameters = new URLSearchParams(window.location.search);
  const debug = parameters.get("debug");
  const query = debug === "1" ? "?debug=1" : "";
  window.history.replaceState({}, document.title, `${window.location.pathname}${query}${window.location.hash}`);
}

async function activeUser(): Promise<User | null> {
  const user = await oidcManager().getUser();
  return user && !user.expired ? user : null;
}

export async function initializeAuthentication(): Promise<AuthenticationState> {
  if (authenticationMode === "development") {
    return { mode: "development", authenticated: true, loginRequired: false, error: "" };
  }
  if (authenticationMode === "unconfigured") {
    return {
      mode: "unconfigured",
      authenticated: false,
      loginRequired: true,
      error: "生产构建缺少 OIDC 配置，请设置 VITE_OIDC_AUTHORITY 和 VITE_OIDC_CLIENT_ID。",
    };
  }
  try {
    const currentManager = oidcManager();
    if (isSigninCallback()) {
      await currentManager.signinCallback();
      cleanCallbackUrl();
    } else if (isSignoutCallback()) {
      await currentManager.signoutRedirectCallback();
      cleanCallbackUrl();
    }
    const user = await activeUser();
    return {
      mode: "oidc",
      authenticated: Boolean(user),
      loginRequired: !user,
      error: "",
    };
  } catch (cause) {
    cleanCallbackUrl();
    return {
      mode: "oidc",
      authenticated: false,
      loginRequired: true,
      error: cause instanceof Error ? cause.message : "企业身份登录回调处理失败",
    };
  }
}

export async function startLogin(): Promise<void> {
  if (authenticationMode !== "oidc") return;
  await oidcManager().signinRedirect({
    state: { returnUrl: `${window.location.pathname}${window.location.search}` },
  });
}

export async function startLogout(): Promise<void> {
  if (authenticationMode !== "oidc") return;
  await oidcManager().signoutRedirect();
}

let renewalInFlight: Promise<boolean> | null = null;

async function performAuthenticationRenewal(): Promise<boolean> {
  const currentManager = oidcManager();
  try {
    const user = await currentManager.signinSilent();
    return Boolean(user && !user.expired);
  } catch {
    // Automatic renewal may have completed while this request was failing.
    const currentUser = await currentManager.getUser();
    if (currentUser?.access_token && !currentUser.expired) return true;
    await currentManager.removeUser();
    window.dispatchEvent(new CustomEvent("erp-authentication-required"));
    return false;
  }
}

export function renewAuthentication(): Promise<boolean> {
  if (authenticationMode !== "oidc") return Promise.resolve(false);
  if (!renewalInFlight) {
    renewalInFlight = performAuthenticationRenewal().finally(() => {
      renewalInFlight = null;
    });
  }
  return renewalInFlight;
}

export async function authorizationHeaders(): Promise<Record<string, string>> {
  if (authenticationMode === "development") {
    const identity = getDevelopmentIdentity();
    return {
      "X-User-Id": identity.userId,
      "X-Tenant-Id": identity.tenantId,
      "X-Org-Code": identity.orgCode,
      "X-Roles": identity.roles.join(","),
    };
  }
  if (authenticationMode === "oidc") {
    let user = await oidcManager().getUser();
    if (user?.expired && await renewAuthentication()) {
      user = await activeUser();
    }
    if (!user?.access_token) {
      window.dispatchEvent(new CustomEvent("erp-authentication-required"));
      throw new Error("登录状态已失效，请重新登录");
    }
    return { Authorization: `${user.token_type || "Bearer"} ${user.access_token}` };
  }
  throw new Error("身份认证尚未配置");
}
