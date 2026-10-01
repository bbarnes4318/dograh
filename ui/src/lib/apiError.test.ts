import { describe, expect, it } from "vitest";

import { detailFromError } from "./apiError";

describe("detailFromError", () => {
  it("returns string details as-is", () => {
    expect(detailFromError({ detail: "Vonage call failed (invalid_number)" })).toBe(
      "Vonage call failed (invalid_number)",
    );
  });

  it("names the field for FastAPI request validation errors", () => {
    const err = {
      detail: [
        {
          loc: ["body", "config", "vonage", "private_key"],
          msg: "Value error, Private key is not a valid PEM private key",
        },
        { loc: ["body", "config", "vonage", "signature_secret"], msg: "Field required" },
      ],
    };
    expect(detailFromError(err)).toBe(
      "private_key: Private key is not a valid PEM private key\nsignature_secret: Field required",
    );
  });

  it("keeps the model prefix for backend validation arrays", () => {
    expect(detailFromError({ detail: [{ model: "llm", message: "bad key" }] })).toBe(
      "llm: bad key",
    );
  });

  it("falls back when nothing is usable", () => {
    expect(detailFromError({}, "fallback")).toBe("fallback");
  });
});
