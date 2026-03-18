import React, { useCallback, useEffect, useRef, useState } from "react";
import { Streamlit, type ComponentProps } from "streamlit-component-lib";
import { Tree } from "react-arborist";
import Node from "./Node";
import type { ArboristAction, ArboristComponentValue, ArboristNode } from "./types";

function clampInt(value: unknown, fallback: number): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.trunc(n);
}

function App(props: ComponentProps) {
  const data = (props.args["data"] as ArboristNode[] | undefined) ?? [];
  // defaultSelection is read ONLY on mount. Python forces a new selection by
  // remounting the component (bumping tree_key / nonce) after structural edits.
  const defaultSelection = (props.args["defaultSelection"] as string | null | undefined) ?? null;
  const height = clampInt(props.args["height"], 600);

  // ── Fully uncontrolled selection — React owns it, Python never writes it back ──
  const [localSelection, setLocalSelection] = useState<string>(defaultSelection ?? "");

  // ── Action tracking (rename/move only) ──
  const nextActionId = useRef<number>(0);

  const report = useCallback(
    (selectedId: string, action: ArboristAction | null, actionId: number) => {
      const value: ArboristComponentValue = { selectedId, lastAction: action, lastActionId: actionId };
      Streamlit.setComponentValue(value);
    },
    [],
  );

  // Report selection whenever it changes.
  const reportedSelection = useRef<string>(defaultSelection ?? "");
  useEffect(() => {
    if (localSelection !== reportedSelection.current) {
      reportedSelection.current = localSelection;
      report(localSelection, null, nextActionId.current);
    }
  }, [localSelection, report]);

  useEffect(() => {
    Streamlit.setFrameHeight(height);
  }, [height]);

  // Initial report on mount so Python gets the default value.
  useEffect(() => {
    report(localSelection, null, 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Tree
      data={data}
      selection={localSelection || undefined}
      openByDefault={true}
      selectionFollowsFocus={false}
      disableMultiSelection={true}
      width={"100%"}
      height={height}
      indent={28}
      rowHeight={28}
      paddingTop={6}
      paddingBottom={6}
      onRename={(args: { id: string; name: string }) => {
        nextActionId.current += 1;
        report(localSelection, { type: "rename", id: String(args.id), name: String(args.name ?? "") }, nextActionId.current);
      }}
      onMove={(args: { dragIds: string[]; parentId: string | null; index: number }) => {
        nextActionId.current += 1;
        report(localSelection, {
          type: "move",
          dragIds: (args.dragIds ?? []).map(String),
          parentId: args.parentId === null ? null : String(args.parentId),
          index: clampInt(args.index, 0),
        }, nextActionId.current);
      }}
    >
      {(nodeProps: any) => (
        <Node
          {...nodeProps}
          onUserSelect={(id: string) => {
            if (id && id !== localSelection) {
              setLocalSelection(id);
            }
          }}
        />
      )}
    </Tree>
  );
}

export default App;

