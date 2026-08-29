// Archived dynamic-PPT builder for historical project reproduction only.
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


async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.plan || !args.output || !args.subtitles) {
    throw new Error("Required arguments: --plan, --output, --subtitles");
  }
  const withSubtitles = args.subtitles === "yes";
  const plan = JSON.parse(await fs.readFile(args.plan, "utf8"));
  if (!Array.isArray(plan.slides) || plan.slides.length === 0) {
    throw new Error("Dynamic PPT plan has no slides");
  }

  const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
  for (const [index, row] of plan.slides.entries()) {
    const slide = presentation.slides.add();
    slide.background.fill = "#000000";
    slide.images.add({
      blob: await readBytes(row.poster_path),
      contentType: "image/jpeg",
      alt: `Video poster ${row.shot_id}`,
      fit: "cover",
      position: { left: 0, top: 0, width: 1280, height: 720 },
    });

    if (withSubtitles && row.subtitle && !row.semantic_card) {
      const layout = row.subtitle_layout ?? {};
      const backdrop = slide.shapes.add({
        geometry: "rect",
        name: `subtitle-backdrop-${String(index + 1).padStart(3, "0")}`,
        position: { left: 0, top: 656, width: 1280, height: 64 },
        fill: { color: "#000000", transparency: 54 },
        line: { style: "solid", fill: "none", width: 0 },
      });
      backdrop.opacity = 0.46;
      const subtitle = slide.shapes.add({
        geometry: "textbox",
        name: `subtitle-${String(index + 1).padStart(3, "0")}`,
        position: { left: 28, top: 658, width: 1224, height: 58 },
        fill: "none",
        line: { style: "solid", fill: "none", width: 0 },
      });
      subtitle.text = layout.clean_text || row.subtitle;
      subtitle.text.style = {
        fontSize: Math.max(14, Number(layout.font_size || 20) * 1.333),
        bold: true,
        color: "#FFFFFF",
        alignment: "center",
        verticalAlignment: "middle",
        fontFamily: "Arial Unicode MS",
      };
    }
  }

  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(args.output);
}


main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
