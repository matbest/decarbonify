export type ArboristNode = {
  id: string;
  name: string;
  icon?: string;
  kind?: string;
  suffix?: string;
  hasType?: boolean;
  retired?: boolean;
  children?: ArboristNode[];
};

/** Actions that require Python-side processing (rename, move). */
export type ArboristAction =
  | { type: "rename"; id: string; name: string }
  | { type: "move"; dragIds: string[]; parentId: string | null; index: number };

/**
 * The component value reported to Python on every setComponentValue call.
 *
 * `selectedId` — current selection (source of truth; no event required).
 * `lastAction` / `lastActionId` — most recent rename/move requiring Python handling.
 */
export type ArboristComponentValue = {
  selectedId: string;
  lastAction: ArboristAction | null;
  lastActionId: number;
};
