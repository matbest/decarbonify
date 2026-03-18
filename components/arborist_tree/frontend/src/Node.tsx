import React, { useEffect, useMemo, useRef } from "react";
import type { NodeRendererProps } from "react-arborist";
import type { ArboristNode } from "./types";

type Props = NodeRendererProps<ArboristNode> & {
  onUserSelect?: (id: string) => void;
};

export default function Node({ node, style, dragHandle, onUserSelect }: Props) {
  const label = useMemo(() => {
    const icon = node.data.icon ? `${node.data.icon} ` : "";
    const kind = node.data.kind ? ` (${node.data.kind})` : "";
    const suffix = node.data.suffix ? ` ${node.data.suffix}` : "";
    return `${icon}${node.data.name}${kind}${suffix}`;
  }, [node.data]);

  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (node.isEditing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [node.isEditing]);

  const textStyle: React.CSSProperties = {
    fontStyle: node.data.hasType === false ? "italic" : undefined,
    textDecoration: node.data.retired ? "line-through" : undefined,
    opacity: node.data.retired ? 0.65 : 1,
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
    fontWeight: node.isSelected ? 600 : undefined,
  };

  const paddingLeft = (() => {
    const base = (style as any)?.paddingLeft;
    if (typeof base === "number") return base + 8;
    if (typeof base === "string" && base.trim()) return `calc(${base} + 8px)`;
    return 8;
  })();

  return (
    <div
      style={{
        ...style,
        display: "flex",
        alignItems: "center",
        paddingLeft,
        paddingRight: 8,
        boxSizing: "border-box",
        cursor: node.isEditing ? "text" : "pointer",
        background: node.willReceiveDrop
          ? "rgba(25, 118, 210, 0.15)"
          : node.isSelected
            ? "rgba(0, 0, 0, 0.08)"
            : "transparent",
        borderLeft: node.isSelected ? "3px solid rgba(0, 0, 0, 0.35)" : "3px solid transparent",
        outline: node.willReceiveDrop ? "2px dashed rgba(25, 118, 210, 0.6)" : "none",
        outlineOffset: "-2px",
        borderRadius: node.willReceiveDrop ? 4 : 0,
      }}
      ref={dragHandle}
      title={label}
      onMouseDown={(e) => {
        if (node.isEditing) return;
        if (e.button !== 0) return;
        if (onUserSelect) {
          onUserSelect(String(node.id));
        }
      }}
      onClick={(e) => {
        // Use the library's built-in selection logic.
        // This avoids odd focus/selection desync issues.
        // (No-op while editing.)
        if (!node.isEditing) {
          e.stopPropagation();
          node.handleClick(e as any);
        }
      }}
      onDoubleClick={(e) => {
        e.stopPropagation();
        node.edit();
      }}
    >
      <div style={{ flex: 1, minWidth: 0, ...textStyle }}>
        {node.isEditing ? (
          <input
            ref={inputRef}
            defaultValue={node.data.name}
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => e.stopPropagation()}
            onBlur={(e) => node.submit(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") node.submit((e.currentTarget as HTMLInputElement).value);
              if (e.key === "Escape") node.reset();
            }}
            style={{ width: "100%" }}
          />
        ) : (
          label
        )}
      </div>
    </div>
  );
}
