import { useEffect, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, useLocation } from "react-router-dom";
import { useConfigStore } from "@/store/configStore";
import { AgentList } from "@/components/AgentList/AgentList";
import { AgentEditor } from "@/components/AgentEditor/AgentEditor";
import { TeamList } from "@/components/TeamList/TeamList";
import { TeamEditor } from "@/components/TeamEditor/TeamEditor";
import { RoomList } from "@/components/RoomList/RoomList";
import { RoomEditor } from "@/components/RoomEditor/RoomEditor";
import { RoomAdmins } from "@/components/RoomAdmins/RoomAdmins";
import { ModelConfig } from "@/components/ModelConfig/ModelConfig";
import { MemoryConfig } from "@/components/MemoryConfig/MemoryConfig";
import { Knowledge } from "@/components/Knowledge/Knowledge";
import { VoiceConfig } from "@/components/VoiceConfig/VoiceConfig";
import { Integrations } from "@/components/Integrations/Integrations";
import { UnconfiguredRooms } from "@/components/UnconfiguredRooms/UnconfiguredRooms";
import { SyncStatus } from "@/components/SyncStatus/SyncStatus";
import { Dashboard } from "@/components/Dashboard/Dashboard";
import { Usage } from "@/components/Usage/Usage";
import { Skills } from "@/components/Skills/Skills";
import { Schedules } from "@/components/Schedules/Schedules";
import { Credentials } from "@/components/Credentials/Credentials";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Toaster } from "@/components/ui/toaster";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { ThemeToggle } from "@/components/ThemeToggle/ThemeToggle";
import { showSaveFailureToastIfNeeded } from "@/components/shared";
import {
  getConfigValidationIssues,
  getGlobalConfigDiagnostics,
  type GlobalConfigDiagnostic,
} from "@/lib/configValidation";
import {
  getNavigationValue,
  NAV_ITEMS,
  Navigation,
} from "@/components/Navigation/Navigation";

const queryClient = new QueryClient();

function RoutePanel({
  active,
  label,
  className,
  children,
}: {
  active: boolean;
  label: string;
  className: string;
  children: ReactNode;
}) {
  if (!active) return null;
  return (
    <section aria-label={`${label} workspace`} className={className}>
      {children}
    </section>
  );
}

function isAuthDiagnosticMessage(message: string): boolean {
  return (
    message.includes("Authentication required") ||
    message.includes("Access denied")
  );
}

export function shouldShowBlockingDiagnosticOverlay(
  blockingDiagnostic: GlobalConfigDiagnostic | null,
  {
    hasLoadedConfig,
    hasRecoveryConfig,
  }: {
    hasLoadedConfig: boolean;
    hasRecoveryConfig: boolean;
  },
): boolean {
  if (blockingDiagnostic == null) {
    return false;
  }
  if (isAuthDiagnosticMessage(blockingDiagnostic.message)) {
    return true;
  }
  return !hasLoadedConfig || hasRecoveryConfig;
}

function AppContent() {
  const {
    loadConfig,
    config,
    recoveryConfigSource,
    recoveryConfigSourceOriginal,
    updateRecoveryConfigSource,
    saveRecoveryConfigSource,
    syncStatus,
    diagnostics,
    configUsesIncludes,
    configJournalPendingRestart,
    isLoading,
    isDirty,
    selectedAgentId,
    selectedTeamId,
    selectedRoomId,
  } = useConfigStore();
  const location = useLocation();

  // Get the current tab from URL or default to 'dashboard'
  const currentTab = getNavigationValue(location.pathname);
  const currentNavItem =
    NAV_ITEMS.find((item) => item.value === currentTab) || NAV_ITEMS[0];
  const validationIssues = getConfigValidationIssues(diagnostics);
  const globalDiagnostics = getGlobalConfigDiagnostics(diagnostics);
  const blockingDiagnostic =
    globalDiagnostics.find(
      (diagnostic) =>
        diagnostic.blocking && isAuthDiagnosticMessage(diagnostic.message),
    ) ??
    globalDiagnostics.find((diagnostic) => diagnostic.blocking) ??
    null;
  const showBlockingDiagnosticOverlay = shouldShowBlockingDiagnosticOverlay(
    blockingDiagnostic,
    {
      hasLoadedConfig: config != null,
      hasRecoveryConfig: recoveryConfigSource != null,
    },
  );
  const canRecoverInvalidConfig =
    !isAuthDiagnosticMessage(blockingDiagnostic?.message ?? "") &&
    recoveryConfigSource != null;
  const recoveryConfigIsDirty =
    recoveryConfigSource !== recoveryConfigSourceOriginal;
  const visibleGlobalDiagnostics = showBlockingDiagnosticOverlay
    ? globalDiagnostics.filter((diagnostic) => !diagnostic.blocking)
    : globalDiagnostics;

  const handleRecoverySave = async () => {
    const result = await saveRecoveryConfigSource();
    showSaveFailureToastIfNeeded(result, {
      staleMessage: "Save was superseded by newer recovery edits.",
      fallbackMessage: "Failed to save replacement configuration.",
    });
  };

  useEffect(() => {
    // Load configuration on mount
    loadConfig();
  }, [loadConfig]);

  const getPlatformUrl = () => {
    const configured = (import.meta as any).env?.VITE_PLATFORM_URL as
      string | undefined;
    if (configured && configured.length > 0) return configured;
    if (typeof window !== "undefined") {
      const host = window.location.host;
      const firstDot = host.indexOf(".");
      const base = firstDot > 0 ? host.slice(firstDot + 1) : host; // 1.staging.mindroom.chat -> staging.mindroom.chat
      return `https://app.${base}`;
    }
    return "https://app.mindroom.chat";
  };

  if (showBlockingDiagnosticOverlay && blockingDiagnostic) {
    const error = blockingDiagnostic.message;
    const isAuthError = isAuthDiagnosticMessage(error);
    const isDifferentInstance = error.includes("Access denied");

    if (!isAuthError && canRecoverInvalidConfig) {
      return (
        <div className="app-shell flex h-screen items-center justify-center">
          <div className="glass-overlay mx-4 w-full max-w-4xl space-y-4 rounded-xl p-5 md:p-6">
            <div className="space-y-2">
              <h2 className="text-xl font-semibold text-gray-900 dark:text-white">
                {validationIssues.length > 0
                  ? "Configuration Validation Failed"
                  : "Configuration Recovery"}
              </h2>
              <p className="text-sm text-gray-600 dark:text-gray-300">
                The current <code>config.yaml</code> could not be loaded. Edit
                the raw configuration below and save it as a full replacement.
              </p>
            </div>

            <div
              role="alert"
              className="rounded-md border border-destructive/20 bg-destructive/5 px-4 py-3 text-sm text-destructive"
            >
              <p className="font-medium">{blockingDiagnostic.message}</p>
              {validationIssues.length > 0 && (
                <ul className="mt-3 list-disc space-y-1 pl-5">
                  {validationIssues.map((issue, index) => (
                    <li key={`${issue.loc.join(".")}-${issue.msg}-${index}`}>
                      <span className="font-medium">
                        {issue.loc.join(" → ") || "config"}
                      </span>
                      {": "}
                      {issue.msg}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <Textarea
              value={recoveryConfigSource}
              onChange={(event) =>
                updateRecoveryConfigSource(event.target.value)
              }
              className="min-h-[420px] font-mono text-sm"
              spellCheck={false}
              disabled={isLoading}
            />

            <div className="flex items-center justify-between gap-3">
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Saving here replaces the entire <code>config.yaml</code> with
                the edited source.
              </p>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  onClick={() => void loadConfig()}
                  disabled={isLoading}
                >
                  Retry
                </Button>
                <Button
                  onClick={() => void handleRecoverySave()}
                  disabled={isLoading || !recoveryConfigIsDirty}
                >
                  Save Replacement Config
                </Button>
              </div>
            </div>
          </div>
        </div>
      );
    }

    return (
      <div className="app-shell flex h-screen items-center justify-center">
        <div className="glass-overlay mx-4 w-full max-w-md rounded-xl p-6">
          <div className="flex items-center mb-4">
            <span className="text-3xl mr-3">🔒</span>
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white">
              {isAuthError ? "Access Required" : "Configuration Error"}
            </h2>
          </div>
          <p className="text-gray-600 dark:text-gray-300 mb-6">{error}</p>

          {!isAuthError && validationIssues.length > 0 && (
            <div className="mb-6 rounded-md border border-destructive/20 bg-destructive/5 px-4 py-3 text-sm text-destructive">
              <p className="font-medium">Current configuration is invalid.</p>
              <p className="mt-1 text-destructive/80">
                Fix the reported issues in <code>config.yaml</code> or the
                referenced plugin manifests, then retry loading the dashboard.
              </p>
              <ul className="mt-3 list-disc space-y-1 pl-5">
                {validationIssues.map((issue, index) => (
                  <li key={`${issue.loc.join(".")}-${issue.msg}-${index}`}>
                    <span className="font-medium">
                      {issue.loc.join(" → ") || "config"}
                    </span>
                    {": "}
                    {issue.msg}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {isAuthError && (
            <div className="space-y-3">
              {isDifferentInstance ? (
                <>
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    You are logged in but do not have access to this instance.
                    You may need to:
                  </p>
                  <ul className="text-sm text-gray-500 dark:text-gray-400 list-disc ml-5 space-y-1">
                    <li>Switch to an instance you have access to</li>
                    <li>Request access from your administrator</li>
                    <li>Return to your dashboard</li>
                  </ul>
                  <a
                    href={`${getPlatformUrl()}/dashboard`}
                    className="block w-full text-center px-4 py-2 bg-primary text-white rounded-md hover:bg-primary/90 transition-colors"
                  >
                    Go to Dashboard
                  </a>
                </>
              ) : (
                <>
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    Please log in to access this MindRoom instance.
                  </p>
                  <a
                    href={`${getPlatformUrl()}/auth/login`}
                    className="block w-full text-center px-4 py-2 bg-primary text-white rounded-md hover:bg-primary/90 transition-colors"
                  >
                    Log In
                  </a>
                </>
              )}
            </div>
          )}

          {!isAuthError && (
            <div className="space-y-3">
              <button
                onClick={() => window.location.reload()}
                className="w-full px-4 py-2 bg-primary text-white rounded-md hover:bg-primary/90 transition-colors"
              >
                Retry
              </button>
              <p className="text-sm text-gray-500 dark:text-gray-400 text-center">
                If the problem persists, please contact support.
              </p>
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="app-shell relative flex h-screen flex-col overflow-hidden">
      <a
        href="#main-content"
        className="glass-control fixed left-3 top-3 z-[100] -translate-y-16 px-3 py-2 text-sm font-medium focus:translate-y-0"
      >
        Skip to content
      </a>
      <header className="shell-toolbar relative z-20 flex h-13 shrink-0 items-center justify-between gap-3 border-b px-3 md:px-5">
        <div className="flex min-w-0 items-center gap-2.5">
          <Navigation mode="mobile" />
          <h1 className="flex min-w-0 items-center gap-2.5">
            <img
              src="/logo.svg"
              alt="MindRoom logo"
              className="h-7 w-7 shrink-0"
            />
            <span className="hidden text-sm font-semibold tracking-tight sm:inline">
              MindRoom
            </span>
          </h1>
          <span aria-hidden="true" className="hidden text-border sm:inline">
            /
          </span>
          <span className="truncate text-sm text-muted-foreground">
            {currentNavItem.label}
          </span>
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          {isDirty && (
            <span className="hidden items-center gap-1.5 text-xs text-muted-foreground sm:flex">
              <span className="h-1.5 w-1.5 rounded-full bg-primary" />
              Draft
            </span>
          )}
          <SyncStatus status={syncStatus} compact className="sm:hidden" />
          <SyncStatus status={syncStatus} className="hidden sm:flex" />
          <ThemeToggle className="glass-control h-9 w-9" />
        </div>
      </header>

      <div className="relative z-10 flex min-h-0 flex-1 flex-col">
        {configUsesIncludes && (
          <div className="border-b border-amber-500/20 bg-amber-500/10 px-3 py-2 text-sm text-amber-900 dark:text-amber-100 sm:px-6">
            This configuration is composed from multiple files via{" "}
            <code>!include</code>. The backend rejects structured saves from the
            dashboard editors — make changes by editing the include source files
            directly.
          </div>
        )}

        {configJournalPendingRestart && (
          <div className="border-b border-amber-500/20 bg-amber-500/10 px-3 py-2 text-sm text-amber-900 dark:text-amber-100 sm:px-6">
            The saved <code>event_journal</code> names a different database from
            the one this process has open. It is read once, when the store is
            opened, so the change takes effect at the next restart — and
            MindRoom refuses to start against a journal it is not bound to, so
            run <code>mindroom journal adopt</code> first if the move is
            deliberate.
          </div>
        )}

        {visibleGlobalDiagnostics.map((diagnostic, index) => (
          <div
            key={`${diagnostic.kind}-${diagnostic.message}-${index}`}
            className="border-b border-destructive/20 bg-destructive/5 px-3 py-2 text-sm text-destructive sm:px-6"
          >
            {diagnostic.message}
          </div>
        ))}

        {config != null && validationIssues.length > 0 && (
          <div className="border-b border-destructive/20 bg-destructive/5 px-3 py-4 text-sm text-destructive sm:px-6">
            <div className="space-y-2">
              <p className="font-medium">
                This draft still has configuration validation issues.
              </p>
              <p className="text-destructive/80">
                Resolve the reported issues in the draft below, then save to
                replace <code>config.yaml</code>.
              </p>
              <ul className="list-disc space-y-1 pl-5">
                {validationIssues.map((issue, index) => (
                  <li key={`${issue.loc.join(".")}-${issue.msg}-${index}`}>
                    <span className="font-medium">
                      {issue.loc.join(" → ") || "config"}
                    </span>
                    {": "}
                    {issue.msg}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}

        <div className="flex min-h-0 flex-1 overflow-hidden">
          <Navigation mode="desktop" />
          <main
            id="main-content"
            tabIndex={-1}
            className="workspace-surface min-w-0 flex-1 overflow-hidden"
          >
            <div className="relative flex h-full flex-col">
              <RoutePanel
                active={currentTab === "dashboard"}
                label="Dashboard"
                className="min-h-0 flex-1 overflow-auto"
              >
                <div className="min-h-full">
                  <Dashboard />
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "usage"}
                label="Usage"
                className="min-h-0 flex-1 overflow-auto p-3 md:p-5"
              >
                <Usage />
              </RoutePanel>

              <RoutePanel
                active={currentTab === "agents"}
                label="Agents"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 sm:gap-4 h-full">
                  <div
                    className={`col-span-1 lg:col-span-4 h-full overflow-hidden ${
                      selectedAgentId ? "hidden lg:block" : "block"
                    }`}
                  >
                    <AgentList />
                  </div>
                  <div
                    className={`col-span-1 lg:col-span-8 h-full overflow-hidden ${
                      selectedAgentId ? "block" : "hidden lg:block"
                    }`}
                  >
                    <AgentEditor />
                  </div>
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "teams"}
                label="Teams"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 sm:gap-4 h-full">
                  <div
                    className={`col-span-1 lg:col-span-4 h-full overflow-hidden ${
                      selectedTeamId ? "hidden lg:block" : "block"
                    }`}
                  >
                    <TeamList />
                  </div>
                  <div
                    className={`col-span-1 lg:col-span-8 h-full overflow-hidden ${
                      selectedTeamId ? "block" : "hidden lg:block"
                    }`}
                  >
                    <TeamEditor />
                  </div>
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "rooms"}
                label="Rooms"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="flex h-full flex-col gap-3 sm:gap-4">
                  <RoomAdmins />
                  <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 sm:gap-4 flex-1 min-h-0">
                    <div
                      className={`col-span-1 lg:col-span-4 h-full overflow-hidden ${
                        selectedRoomId ? "hidden lg:block" : "block"
                      }`}
                    >
                      <RoomList />
                    </div>
                    <div
                      className={`col-span-1 lg:col-span-8 h-full overflow-hidden ${
                        selectedRoomId ? "block" : "hidden lg:block"
                      }`}
                    >
                      <RoomEditor />
                    </div>
                  </div>
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "schedules"}
                label="Schedules"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="h-full overflow-hidden">
                  <Schedules />
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "unconfigured-rooms"}
                label="External rooms"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="h-full overflow-hidden">
                  <UnconfiguredRooms />
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "models"}
                label="Models"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="h-full overflow-hidden">
                  <ModelConfig />
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "memory"}
                label="Memory"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="h-full overflow-hidden">
                  <MemoryConfig />
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "knowledge"}
                label="Knowledge"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="h-full overflow-hidden">
                  <Knowledge />
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "credentials"}
                label="Credentials"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="h-full overflow-hidden">
                  <Credentials />
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "voice"}
                label="Voice"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="h-full overflow-auto">
                  <VoiceConfig />
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "integrations"}
                label="Tools"
                className="min-h-0 flex-1 overflow-auto p-3 md:p-5"
              >
                <div className="h-full overflow-auto">
                  <Integrations />
                </div>
              </RoutePanel>

              <RoutePanel
                active={currentTab === "skills"}
                label="Skills"
                className="min-h-0 flex-1 overflow-hidden p-3 md:p-5"
              >
                <div className="h-full overflow-hidden">
                  <Skills />
                </div>
              </RoutePanel>
            </div>
          </main>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <AppContent />
          <Toaster />
        </ThemeProvider>
      </QueryClientProvider>
    </BrowserRouter>
  );
}
