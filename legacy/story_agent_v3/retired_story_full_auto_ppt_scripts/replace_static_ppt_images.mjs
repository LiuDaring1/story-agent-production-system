#!/usr/bin/env node
/** Archived image-swap helper for historical project reproduction only. */

import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

function parseArgs(argv) {
  const out = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index]?.replace(/^--/, "");
    const value = argv[index + 1];
    if (!key || value === undefined) throw new Error(`Invalid arguments near ${argv[index]}`);
    out[key] = value;
  }
  for (const key of ["input", "plan", "output", "preview-dir", "layout-dir", "inspect-out"]) {
    if (!out[key]) throw new Error(`Missing --${key}`);
  }
  return out;
}

async function importRuntimeModule(packageName) {
  const nodeModules = process.env.RUNTIME_NODE_MODULES;
  if (!nodeModules || !path.isAbsolute(nodeModules)) {
    throw new Error("RUNTIME_NODE_MODULES must be an absolute path");
  }
  const requireFromRuntime = createRequire(path.join(nodeModules, "__runtime__.cjs"));
  const entrypoint = requireFromRuntime.resolve(packageName);
  return import(pathToFileURL(entrypoint).href);
}

function slidesOf(presentation) {
  if (Array.isArray(presentation.slides?.items)) return presentation.slides.items;
  if (Number.isInteger(presentation.slides?.count) && typeof presentation.slides.getItem === "function") {
    return Array.from({ length: presentation.slides.count }, (_, index) => presentation.slides.getItem(index));
  }
  throw new Error("Could not enumerate slides");
}

function imagesOf(slide) {
  if (Array.isArray(slide.images?.items)) return slide.images.items;
  if (Number.isInteger(slide.images?.count) && typeof slide.images.getItem === "function") {
    return Array.from({ length: slide.images.count }, (_, index) => slide.images.getItem(index));
  }
  throw new Error("Could not enumerate slide images");
}

async function saveBlob(blob, target) {
  await fs.mkdir(path.dirname(target), { recursive: true });
  if (blob && typeof blob.arrayBuffer === "function") {
    await fs.writeFile(target, Buffer.from(await blob.arrayBuffer()));
    return;
  }
  await fs.writeFile(target, Buffer.from(blob));
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const { FileBlob, PresentationFile } = await importRuntimeModule("@oai/artifact-tool");
  const plan = JSON.parse(await fs.readFile(args.plan, "utf8"));
  const starterPptxPath = args.input;
  if (!Array.isArray(plan.slides) || plan.slides.length < 2 || plan.slides[0]?.shot_id !== "TITLE") {
    throw new Error("Plan must contain TITLE followed by one or more director-shot pages");
  }
  const storySlides = new Map(
    plan.slides
      .filter((row) => row.shot_id && row.shot_id !== "TITLE")
      .map((row) => [Number(row.slide_index), row]),
  );
  const presentation = await PresentationFile.importPptx(await FileBlob.load(starterPptxPath));
  const slides = slidesOf(presentation);
  if (slides.length !== plan.slides.length || storySlides.size !== slides.length - 1) {
    throw new Error(`Template and plan page counts differ; deck=${slides.length}, plan=${plan.slides.length}`);
  }

  for (let slideIndex = 2; slideIndex <= slides.length; slideIndex += 1) {
    const row = storySlides.get(slideIndex);
    if (!row) throw new Error(`Missing plan row for slide ${slideIndex}`);
    const images = imagesOf(slides[slideIndex - 1]);
    if (images.length !== 1) throw new Error(`Slide ${slideIndex} must contain exactly one inherited image; got ${images.length}`);
    const image = images[0];
    const oldFrame = image.frame;
    const oldCrop = image.crop;
    const oldFit = image.fit;
    const oldAlt = image.alt;
    const oldGeometry = image.geometry;
    const oldBorderRadius = image.borderRadius;
    const oldRotation = image.rotation;
    const oldFlipHorizontal = image.flipHorizontal;
    const oldFlipVertical = image.flipVertical;
    const oldLockAspectRatio = image.lockAspectRatio;
    const replacementBytes = await fs.readFile(row.poster_path);
    image.replace({
      blob: replacementBytes,
      contentType: "image/png",
      alt: oldAlt || `${plan.story_name || "Story"} ${row.shot_id} 导演镜头插画`,
      ...(oldFit ? { fit: oldFit } : {}),
      prompt: row.imagegen_prompt,
    });
    image.frame = oldFrame;
    image.crop = oldCrop;
    image.geometry = oldGeometry;
    image.borderRadius = oldBorderRadius;
    image.rotation = oldRotation;
    image.flipHorizontal = oldFlipHorizontal;
    image.flipVertical = oldFlipVertical;
    image.lockAspectRatio = oldLockAspectRatio;
  }

  await fs.mkdir(args["preview-dir"], { recursive: true });
  await fs.mkdir(args["layout-dir"], { recursive: true });
  for (let index = 0; index < slides.length; index += 1) {
    const slideNo = String(index + 1).padStart(2, "0");
    await saveBlob(
      await presentation.export({ slide: slides[index], format: "png", scale: 1 }),
      path.join(args["preview-dir"], `slide-${slideNo}.png`),
    );
    await saveBlob(
      await presentation.export({ slide: slides[index], format: "layout" }),
      path.join(args["layout-dir"], `slide-${slideNo}.layout.json`),
    );
  }
  const inspect = await presentation.inspect({ kind: "slide,image,shape,textbox", maxChars: 250000 });
  await fs.writeFile(args["inspect-out"], String(inspect), "utf8");
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(args.output);
  console.log(JSON.stringify({ status: "ok", slideCount: slides.length, output: args.output }, null, 2));
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});
