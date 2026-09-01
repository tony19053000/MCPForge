import { AuthProviderContext } from "@/lib/auth/context";
import { WorkspaceView } from "@/components/workspace/workspace-view";

export const metadata = { title: "Workspace · MCPForge" };

export default function WorkspacePage() {
  return (
    <AuthProviderContext>
      <WorkspaceView />
    </AuthProviderContext>
  );
}
