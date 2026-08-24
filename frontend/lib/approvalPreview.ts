export interface ApprovalPreviewCard<T> {
  tool_name: string;
  preview: {
    deletes?: T[];
    updates?: T[];
  };
}

export function affectedTasks<T>(card: ApprovalPreviewCard<T>): T[] {
  switch (card.tool_name) {
    case "delete_tasks":
      return card.preview.deletes ?? [];
    case "bulk_update_tasks":
      return card.preview.updates ?? [];
    default:
      return [];
  }
}
