/**
 * The browser agent's model-visible capability profile, for display.
 *
 * These are the eight tools `build_agent` registers for the browser profile,
 * which is `ALL_TOOLS`, which is every member of the backend `ToolName` enum.
 * The Linear profile is deliberately smaller and must never drive this list.
 *
 * The list is written out rather than derived at runtime, and the reason is the
 * trust boundary. The authoritative profile is server-side; the browser is not
 * told which tools exist, and a client-supplied AG-UI `tools` array is rebuilt
 * empty on the way in precisely so that a client claim about capabilities
 * cannot become authority. Reading the display list from that array would
 * quietly reverse the direction the boundary runs in.
 *
 * A written-out list can go stale, so a test reads the backend enum and fails
 * when the two disagree. That is the half a frontend-only assertion cannot do:
 * eight hardcoded labels checked against themselves stay green forever while a
 * ninth tool lands in the backend and the header quietly lies.
 *
 * Order is presentation, not the enum's declaration order. It runs from the
 * operations someone being shown the product recognises first to the ones that
 * need explaining.
 */
export const AGENT_TOOL_LABELS = [
  "Create Task",
  "Update Task",
  "List Tasks",
  "Bulk Update Tasks",
  "Delete Tasks",
  "Get Task History",
  "Resolve Task Reference",
  "Propose Plan",
] as const;
