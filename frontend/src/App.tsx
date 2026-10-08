import { useEffect, useState, type KeyboardEvent } from "react";
import { ThemeProvider } from "next-themes";
import { useMsal } from "@azure/msal-react";
import { LayoutDashboard, LogOut, MessageSquare, Settings, User } from "lucide-react";

import { ThemeToggle } from "./components/ThemeToggle";
import { OverviewTab } from "./components/OverviewTab";
import { ConversationsTab } from "./components/ConversationsTab";
import { ConfigurationTab } from "./components/ConfigurationTab";
import { SignInGate } from "./components/SignInGate";
import { fetchVersion, type AuthConfig } from "./lib/api";

type Tab = "overview" | "conversations" | "configuration";
const TAB_ORDER: Tab[] = ["overview", "conversations", "configuration"];

interface AppProps {
  authConfig: AuthConfig;
}

/**
 * User chip in the header. Rendered only when auth is enabled -- when the
 * dashboard is served without auth there is nothing meaningful to display.
 * When rendered, it exposes the signed-in account name and a Sign out
 * button that calls MSAL `logoutRedirect`.
 */
function UserChip() {
  const { instance, accounts } = useMsal();
  const account = instance.getActiveAccount() ?? accounts[0] ?? null;
  if (!account) return null;
  return (
    <div className="flex items-center gap-2 rounded-full border border-border bg-muted/40 py-1 pl-2 pr-1 text-xs">
      <User className="h-3.5 w-3.5 text-muted-foreground" />
      <span className="max-w-[16rem] truncate font-medium text-foreground">
        {account.name || account.username}
      </span>
      <button
        type="button"
        onClick={() => void instance.logoutRedirect({ account })}
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-muted-foreground transition hover:bg-background hover:text-foreground"
        title="Sign out"
      >
        <LogOut className="h-3.5 w-3.5" />
        <span>Sign out</span>
      </button>
    </div>
  );
}

function Dashboard({ authConfig }: { authConfig: AuthConfig }) {
  const [tab, setTab] = useState<Tab>("overview");
  const [version, setVersion] = useState("");

  useEffect(() => {
    fetchVersion().then(setVersion).catch(() => setVersion(""));
  }, []);

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    let nextTab: Tab | undefined;
    const currentIndex = TAB_ORDER.indexOf(tab);
    if (event.key === "ArrowRight") {
      nextTab = TAB_ORDER[(currentIndex + 1) % TAB_ORDER.length];
    } else if (event.key === "ArrowLeft") {
      nextTab = TAB_ORDER[(currentIndex - 1 + TAB_ORDER.length) % TAB_ORDER.length];
    } else if (event.key === "Home") {
      nextTab = TAB_ORDER[0];
    } else if (event.key === "End") {
      nextTab = TAB_ORDER[TAB_ORDER.length - 1];
    }

    if (nextTab) {
      event.preventDefault();
      setTab(nextTab);
      document.getElementById(`dashboard-tab-${nextTab}`)?.focus();
    }
  }

  return (
    <div className="mx-auto min-h-screen max-w-7xl px-4 py-6">
      <header className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <img src="/logo.png" alt="Agent Landing Zone" className="h-10 w-10" />
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Agent Landing Zone Orchestrator</h1>
            {version && (
              <span className="text-xs text-muted-foreground">v{version}</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          {authConfig.authEnabled && <UserChip />}
          <ThemeToggle />
        </div>
      </header>

      <SignInGate authConfig={authConfig}>
        <nav className="mb-4 flex gap-1 border-b" role="tablist" aria-label="Dashboard sections">
          <button
            id="dashboard-tab-overview"
            type="button"
            role="tab"
            aria-selected={tab === "overview"}
            aria-controls="dashboard-tabpanel"
            tabIndex={tab === "overview" ? 0 : -1}
            onKeyDown={handleTabKeyDown}
            onClick={() => setTab("overview")}
            className={`flex items-center gap-1.5 border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
              tab === "overview"
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <LayoutDashboard className="h-4 w-4" />
            Overview
          </button>
          <button
            id="dashboard-tab-conversations"
            type="button"
            role="tab"
            aria-selected={tab === "conversations"}
            aria-controls="dashboard-tabpanel"
            tabIndex={tab === "conversations" ? 0 : -1}
            onKeyDown={handleTabKeyDown}
            onClick={() => setTab("conversations")}
            className={`flex items-center gap-1.5 border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
              tab === "conversations"
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <MessageSquare className="h-4 w-4" />
            Conversations
          </button>
          <button
            id="dashboard-tab-configuration"
            type="button"
            role="tab"
            aria-selected={tab === "configuration"}
            aria-controls="dashboard-tabpanel"
            tabIndex={tab === "configuration" ? 0 : -1}
            onKeyDown={handleTabKeyDown}
            onClick={() => setTab("configuration")}
            className={`flex items-center gap-1.5 border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
              tab === "configuration"
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Settings className="h-4 w-4" />
            Configuration
          </button>
        </nav>

        <div
          id="dashboard-tabpanel"
          role="tabpanel"
          aria-labelledby={`dashboard-tab-${tab}`}
        >
          {tab === "overview" && <OverviewTab />}
          {tab === "conversations" && <ConversationsTab />}
          {tab === "configuration" && <ConfigurationTab />}
        </div>
      </SignInGate>
    </div>
  );
}

export default function App({ authConfig }: AppProps) {
  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
      <Dashboard authConfig={authConfig} />
    </ThemeProvider>
  );
}
