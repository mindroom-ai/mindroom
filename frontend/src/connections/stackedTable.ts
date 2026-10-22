/**
 * Tailwind classes that stack a table into cards below 640 px (`sm`) and leave
 * it as it is above. A cell that has a header says so with `data-label` and
 * `STACKED_LABEL`, since the header row is only read aloud there.
 */
export const STACKED = {
  table: "max-sm:block",
  head: "max-sm:sr-only",
  body: "max-sm:block",
  row: "max-sm:block",
} as const;

export const STACKED_CELL = "max-sm:block";

export const STACKED_LABEL =
  "max-sm:before:mb-0.5 max-sm:before:block max-sm:before:text-xs max-sm:before:font-sans max-sm:before:font-medium max-sm:before:text-muted-foreground max-sm:before:content-[attr(data-label)]";
