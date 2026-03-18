import React, { useCallback, useEffect, useRef, useState } from "react";
import { Streamlit, type ComponentProps } from "streamlit-component-lib";
import { Tree } from "react-arborist";
import Node from "./Node";
import type { ArboristAction, ArboristComponentValue, ArboristNode } from "./types";

/**
 * Mirror of Python's can_add_child containment rules.
 * Returns true if dropping dragNodes onto parentNode should be BLOCKED.
 */
function shouldDisableDrop({
  parentNode,
  dragNodes,
}: {
  parentNode: { data: ArboristNode; isRoot: boolean };
  dragNodes: { data: ArboristNode }[];
  index: number;
}): boolean {
  // Root (portfolio level) accepts anything.
  if (parentNode.isRoot) return false;

  const pCat = parentNode.data.cat ?? "other";

  for (const drag of dragNodes) {
    const cCat = drag.data.cat ?? "other";

    // "component" category (generic asset, energy_system, etc.) is always allowed anywhere.
    if (pCat === "component" && cCat === "component") continue;
    if (pCat === "other" || cCat === "other") continue;

    // Buildings cannot contain land or other buildings.
    if (pCat === "building" && (cCat === "land" || cCat === "building")) return true;
    // Rooms cannot contain buildings or land.
    if (pCat === "room" && (cCat === "building" || cCat === "land")) return true;
    // Components/equipment cannot contain places.
    if (pCat === "component" && (cCat === "room" || cCat === "building" || cCat === "land" || cCat === "place")) return true;
  }
  return false;
}

function clampInt(value: unknown, fallback: number): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.trunc(n);
}


function App(props: ComponentProps) {
  const data = (props.args["data"] as ArboristNode[] | undefined) ?? [];
  const defaultSelection = (props.args["defaultSelection"] as string | null | undefined) ?? null;
  const height = clampInt(props.args["height"], 600);

  const [localSelection, setLocalSelection] = useState<string>(defaultSelection ?? "");
  const [errors, setErrors] = useState<string[]>([]);
  const nextActionId = useRef<number>(0);

  const report = useCallback(
    (selectedId: string, action: ArboristAction | null, actionId: number, error?: string) => {
      let newErrors = errors;
      if (error) {
        newErrors = [...errors, error];
        setErrors(newErrors);
      }
      const value: ArboristComponentValue = { selectedId, lastAction: action, lastActionId: actionId, errors: newErrors };
      Streamlit.setComponentValue(value);
    },
    [errors],
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
      disableDrop={(args) => {
        const blocked = shouldDisableDrop(args);
        if (blocked) {
          // Compose a user-friendly error message.
          const parent = args.parentNode?.data?.name || "target";
          const dragNames = (args.dragNodes || []).map(n => n.data?.name || "item").join(", ");
          report(localSelection, null, nextActionId.current, `Cannot move ${dragNames} into ${parent}: not allowed by containment rules.`);
        }
        return blocked;
      }}
      width={"100%"}
      height={height}
      indent={28}
      rowHeight={36}
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

