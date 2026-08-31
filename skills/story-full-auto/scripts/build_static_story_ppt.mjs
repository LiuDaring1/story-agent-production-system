import fs from "node:fs/promises";
import process from "node:process";
import { Presentation, PresentationFile } from "@oai/artifact-tool";


function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error(`Invalid argument list near ${key ?? "<end>"}`);
    }
    result[key.slice(2)] = value;
  }
  return result;
}


async function readBytes(path) {
  const bytes = await fs.readFile(path);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}


async function writeBlob(path, blob) {
  await fs.writeFile(path, new Uint8Array(await blob.arrayBuffer()));
}


async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.plan || !args.output || !args.subtitles || !args.evidence) {
    throw new Error("Required: --plan, --output, --subtitles, --evidence");
  }
  const withSubtitles = args.subtitles === "yes";
  const plan = JSON.parse(await fs.readFile(args.plan, "utf8"));
  if (!Array.isArray(plan.slides) || plan.slides.length === 0) {
    throw new Error("Static PPT plan has no slides");
  }
  await fs.mkdir(args.evidence, { recursive: true });

  const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
  for (const [index, row] of plan.slides.entries()) {
    const slide = presentation.slides.add();
    slide.background.fill = "#000000";
    const imageBytes = await readBytes(row.poster_path);
    slide.images.add({
      blob: imageBytes,
      contentType: row.poster_path.toLowerCase().endsWith(".png") ? "image/png" : "image/jpeg",
      alt: `Static story image ${row.shot_id}`,
      fit: "cover",
      position: { left: 0, top: 0, width: 1280, height: 720 },
    });

    if (withSubtitles && row.shot_id !== "TITLE" && row.shot_id !== "MORAL" && String(row.subtitle || "").trim()) {
      // TXT line boundaries are authoritative. A slide consumes exactly one
      // cue and displays it on one line; it must never rewrap into the old
      // oversized two-line block.
      const text = String(row.subtitle).replace(/[\r\n]+/g, "").trim();
      const barHeight = 78;
      const fontSize = Math.max(24, Math.min(38, Math.floor(1180 / Math.max(1, text.length))));
      const backdrop = slide.shapes.add({
        geometry: "rect",
        name: `subtitle-backdrop-${String(index + 1).padStart(3, "0")}`,
        position: { left: 0, top: 720 - barHeight, width: 1280, height: barHeight },
        fill: "#000000/58",
        line: { style: "solid", fill: "none", width: 0 },
      });
      backdrop.opacity = 0.58;
      const subtitle = slide.shapes.add({
        geometry: "textbox",
        name: `subtitle-${String(index + 1).padStart(3, "0")}`,
        position: { left: 42, top: 720 - barHeight + 5, width: 1196, height: barHeight - 10 },
        fill: "none",
        line: { style: "solid", fill: "none", width: 0 },
      });
      subtitle.text = text;
      subtitle.text.style = {
        fontSize,
        bold: true,
        color: "#FFFFFF",
        alignment: "center",
        verticalAlignment: "middle",
        typeface: "Source Han Sans",
        autoFit: "shrinkText",
        wrap: "none",
        insets: { top: 2, right: 6, bottom: 2, left: 6 },
      };
    }
  }

  for (const [index, slide] of presentation.slides.items.entries()) {
    const stem = `slide-${String(index + 1).padStart(2, "0")}`;
    await writeBlob(`${args.evidence}/${stem}.png`, await presentation.export({ slide, format: "png", scale: 1 }));
    const layout = await slide.export({ format: "layout" });
    await fs.writeFile(`${args.evidence}/${stem}.layout.json`, await layout.text());
  }
  await writeBlob(
    `${args.evidence}/deck-montage.webp`,
    await presentation.export({ format: "webp", montage: true, scale: 1 }),
  );
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(args.output);
}


main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
