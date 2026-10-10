import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, LogOut, MessageSquare, ScrollText, Server as ServerIcon, Store as StoreIcon, Users } from "lucide-react";
import type { ReactNode } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";

import { api, ApiError, type User } from "./api";
import { Badge } from "./components/ui";
import { AcceptInvitePage } from "./pages/AcceptInvite";
import { AuditPage } from "./pages/Audit";
import { ChatPage } from "./pages/Chat";
import { KeysPage } from "./pages/Keys";
import { LoginPage } from "./pages/Login";
import { ServersPage } from "./pages/Servers";
import { StoresPage } from "./pages/Stores";
import { TeamPage } from "./pages/Team";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/invite/:token" element={<AcceptInvitePage />} />
      <Route path="/*" element={<SignedIn />} />
    </Routes>
  );
}

/** Everything behind sign-in. Asks the gateway who we are before showing anything. */
function SignedIn() {
  const location = useLocation();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });

  if (me.isPending) return <p className="p-8 text-sm text-slate-500">Loading…</p>;
  if (me.error) {
    if (me.error instanceof ApiError && me.error.status === 401) {
      return <Navigate to="/login" replace state={{ from: location.pathname }} />;
    }
    return <p className="p-8 text-sm text-rose-700">The gateway cannot be reached: {me.error.message}</p>;
  }
  const user = me.data;

  return (
    <div className="flex min-h-screen">
      <Sidebar user={user} />
      <main className="min-w-0 flex-1 px-4 py-6 md:px-8 md:py-8">
        <div className="mx-auto max-w-5xl">
          <Routes>
            <Route path="/" element={<Navigate to="/servers" replace />} />
            <Route path="/servers" element={<ServersPage user={user} />} />
            <Route path="/stores" element={<StoresPage user={user} />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/keys" element={<KeysPage />} />
            <Route path="/audit" element={<AuditPage user={user} />} />
            {user.role === "admin" && <Route path="/team" element={<TeamPage user={user} />} />}
            <Route path="*" element={<Navigate to="/servers" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}

function Sidebar({ user }: { user: User }) {
  const queryClient = useQueryClient();
  // The Stores page is offered only where the deployment runs the provisioner.
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      queryClient.clear();
      window.location.assign("/login");
    },
  });

  return (
    <aside className="flex w-14 shrink-0 flex-col border-r border-slate-200 bg-white md:w-60">
      {/* On a narrow window the sidebar shrinks to its icons. */}
      <div className="hidden px-5 py-5 md:block">
        <div className="text-base font-semibold text-slate-900">MCP Gateway</div>
        <div className="text-xs text-slate-500">One endpoint for the team's tools</div>
      </div>
      <nav className="flex-1 space-y-1 px-2 pt-3 md:px-3 md:pt-0">
        <Item to="/servers" icon={<ServerIcon size={16} />} label="Servers" />
        {config.data?.stores_enabled && <Item to="/stores" icon={<StoreIcon size={16} />} label="Stores" />}
        <Item to="/chat" icon={<MessageSquare size={16} />} label="Chat" />
        <Item to="/keys" icon={<KeyRound size={16} />} label="API keys" />
        {user.role === "admin" && <Item to="/team" icon={<Users size={16} />} label="Team" />}
        <Item to="/audit" icon={<ScrollText size={16} />} label="Audit log" />
      </nav>
      <div className="border-t border-slate-200 px-2 py-4 md:px-4">
        <div className="hidden md:block">
          <div className="truncate text-sm font-medium text-slate-900">{user.name}</div>
          <div className="mb-2 truncate text-xs text-slate-500">{user.email}</div>
        </div>
        <div className="flex items-center justify-between">
          <span className="hidden md:inline">
            <Badge tone={user.role === "admin" ? "blue" : "neutral"}>{user.role}</Badge>
          </span>
          <button
            type="button"
            onClick={() => logout.mutate()}
            title="Sign out"
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-slate-600 hover:bg-slate-100"
          >
            <LogOut size={14} /> <span className="hidden md:inline">Sign out</span>
          </button>
        </div>
      </div>
    </aside>
  );
}

function Item({ to, icon, label }: { to: string; icon: ReactNode; label: string }) {
  return (
    <NavLink
      to={to}
      title={label}
      className={({ isActive }) =>
        `flex items-center justify-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium md:justify-start ${
          isActive ? "bg-indigo-50 text-indigo-700" : "text-slate-700 hover:bg-slate-100"
        }`
      }
    >
      {icon}
      <span className="hidden md:inline">{label}</span>
    </NavLink>
  );
}
