// Supabase Edge Function: generate-pet
// Triggered by database webhook on INSERT into pet_jobs table.
// Generates pet pixel art image via OpenAI and saves to Supabase Storage.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const OPENAI_API_KEY = Deno.env.get("OPENAI_API_KEY")!;

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

function sanitizeEmail(email: string): string {
  return email.toLowerCase().trim().replace(/[^a-zA-Z0-9_-]/g, "_");
}

interface WebhookPayload {
  type: "INSERT";
  table: string;
  record: {
    id: number;
    user_email: string;
    slot_id: number;
    job_type: string;
    status: string;
  };
}

Deno.serve(async (req) => {
  try {
    const payload: WebhookPayload = await req.json();
    const { id: jobId, user_email, slot_id, job_type } = payload.record;

    if (payload.record.status !== "pending") {
      return new Response(JSON.stringify({ ok: true, skipped: true }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // Mark job as processing
    await supabase
      .from("pet_jobs")
      .update({ status: "processing" })
      .eq("id", jobId);

    // Get slot data
    const { data: slotData } = await supabase
      .from("plant_slots")
      .select("*")
      .eq("user_email", user_email)
      .eq("slot_id", slot_id)
      .single();

    if (!slotData) {
      throw new Error("Slot not found");
    }

    const petConfig = slotData.pet_config || {};
    const plantProfile = slotData.plant_profile || {};
    const petState = slotData.pet_state || {};

    if (!petConfig.name) {
      throw new Error("Pet not configured");
    }

    const petName = petConfig.name || "Mimi";
    const petType = petConfig.type === "cat" ? "cat" : "dog";
    const petTypePt = petConfig.type === "cat" ? "gato" : "cachorro";
    const plantName = plantProfile.nome_popular || "planta";

    // Get current sensor data
    const { data: sensorData } = await supabase
      .from("sensor_readings")
      .select("*")
      .order("timestamp", { ascending: false })
      .limit(1);

    const sensor = sensorData?.[0];
    const temp = sensor?.temperature ?? 22;
    const hum = sensor?.humidity ?? 50;
    const soil = sensor?.soil_moisture ?? 30;

    // Calculate health score
    const idealTemp = [
      plantProfile.temperatura_ideal_min ?? 18,
      plantProfile.temperatura_ideal_max ?? 28,
    ];
    const idealHum = [
      plantProfile.umidade_ar_ideal_min ?? 40,
      plantProfile.umidade_ar_ideal_max ?? 70,
    ];
    const idealSoil = [
      plantProfile.umidade_solo_ideal_min ?? 15,
      plantProfile.umidade_solo_ideal_max ?? 55,
    ];

    let healthScore = 100;
    if (temp < idealTemp[0]) healthScore -= Math.min(30, (idealTemp[0] - temp) * 5);
    else if (temp > idealTemp[1]) healthScore -= Math.min(30, (temp - idealTemp[1]) * 5);
    if (hum < idealHum[0]) healthScore -= Math.min(20, (idealHum[0] - hum) * 2);
    else if (hum > idealHum[1]) healthScore -= Math.min(10, hum - idealHum[1]);
    if (soil < idealSoil[0]) healthScore -= Math.min(50, (idealSoil[0] - soil) * 5);
    else if (soil > idealSoil[1]) healthScore -= Math.min(30, (soil - idealSoil[1]) * 3);
    healthScore = Math.max(0, Math.min(100, Math.round(healthScore)));

    // Time period
    const now = new Date();
    const hour = now.getUTCHours() - 3; // BRT approximation
    let timePeriod: string;
    if (hour >= 5 && hour < 9) timePeriod = "dawn with soft golden light and morning dew on leaves";
    else if (hour >= 9 && hour < 12) timePeriod = "bright sunny morning, cheerful warm light";
    else if (hour >= 12 && hour < 15) timePeriod = "midday with strong overhead sun";
    else if (hour >= 15 && hour < 18) timePeriod = "warm golden afternoon light, long soft shadows";
    else if (hour >= 18 && hour < 21) timePeriod = "dusk with purple-orange gradient sky";
    else timePeriod = "cozy night scene with moonlight and twinkling stars";

    // Pet action based on sensor state
    let action: string;
    const actions85 = [
      "playing a tiny ukulele and serenading the thriving plant with a happy song",
      "doing an excited victory dance next to the lush healthy plant",
      "polishing each leaf gently with a tiny soft cloth, humming contentedly",
      "measuring the plant height with a mini ruler, beaming with pride",
      "taking a selfie with the plant using a tiny smartphone, both looking adorable",
      "placing a tiny golden trophy next to the plant, celebrating its perfect health",
      "watering with a fancy can while whistling, doing a little dance step",
    ];
    const actionsDefault = [
      "carefully checking soil moisture with a tiny probe, looking studious",
      "adding plant food pellets to the soil, reading the instructions",
      "gently trimming a leaf with tiny scissors, being very precise",
      "consulting a tiny plant care handbook with a focused expression",
      "giving the plant an encouraging thumbs up and a warm determined smile",
    ];

    if (soil < 15) action = "frantically running with an oversized watering can, clearly panicking to save the extremely dry plant";
    else if (soil < idealSoil[0]) action = "carefully watering the plant with a tiny cute watering can, very focused and gentle";
    else if (soil > 60) action = "holding a tiny umbrella over the plant, looking worried about excess water";
    else if (temp < idealTemp[0]) action = "wrapping the plant pot in a tiny cozy scarf and blanket, shivering adorably";
    else if (temp > idealTemp[1]) action = "fanning the plant with a giant leaf fan, sweating with effort";
    else if (hum < idealHum[0]) action = "misting the air around the plant with a cute spray bottle, being very thorough";
    else if (healthScore >= 85) action = actions85[Math.floor(Math.random() * actions85.length)];
    else action = actionsDefault[Math.floor(Math.random() * actionsDefault.length)];

    const moodSuffix = healthScore >= 85
      ? "with sparkling star-shaped eyes, radiating pure joy and pride"
      : healthScore < 40
      ? "with a deeply worried furrowed brow and tiny stress sweat drops"
      : "with a caring determined expression and focused anime eyes";

    let plantVisual: string;
    if (healthScore >= 80) plantVisual = "super healthy — lush deep green leaves, sparkling water droplets, tiny glowing sparkle effects radiating vitality";
    else if (healthScore >= 50) plantVisual = "healthy and green, looking well and cared for";
    else if (healthScore >= 30) plantVisual = "wilting slightly, pale drooping leaves, clearly needing attention";
    else plantVisual = "critically struggling — brown leaf tips, dramatically drooping, dry cracked soil, desperate for care";

    // Web search for event of the day
    let eventText = "";
    try {
      const eventResp = await fetch("https://api.openai.com/v1/responses", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${OPENAI_API_KEY}`,
        },
        body: JSON.stringify({
          model: "gpt-4.1-mini",
          tools: [{ type: "web_search" }],
          input: `Hoje é ${now.toLocaleDateString("pt-BR", { day: "numeric", month: "long", year: "numeric" })}. Encontre 1 evento curioso, engraçado ou feriado de hoje. Responda em uma frase curta.`,
        }),
      });
      const eventData = await eventResp.json();
      eventText = eventData.output_text || "";
    } catch {
      // Web search is optional
    }

    const eventElement = eventText
      ? ` Include a subtle fun prop referencing today's special event: ${eventText} (e.g., a tiny themed hat or item on the ground).`
      : "";

    let creativePrompt = `MAXIMUM CUTENESS retro pixel art sprite of an EXTREMELY ADORABLE chubby chibi ${petType} named '${petName}' ${action}, ${moodSuffix}. CUTENESS RULES: oversized round head (60% of body), body is a soft squishy ball shape, tiny stubby legs, GIGANTIC pixel eyes with a single tiny 1-pixel white highlight dot (NO glare, NO bloom, NO heavy shine), rosy blushing cheeks, small cute mouth with a tiny smile or worried pout. Irresistibly kawaii — like a premium Tamagotchi or Pokémon sprite. The ${petType} is beside a ${plantName} plant with heart-shaped leaves on a dark vertical trellis in a terracotta pot. PIXEL ART STYLE: NES / Game Boy Color / early SNES era — chunky visible square pixels, bold black pixel outlines on every shape, flat color fills, NO gradients, NO soft shading. Limited palette 16-32 colors. Hard pixel edges. Raw classic retro videogame sprite quality. CHARACTER CONSISTENCY RULE: The ${petType}'s fur color, fur pattern, markings, eye color and body proportions are LOCKED to the name '${petName}' — every generation with this name MUST produce the IDENTICAL character design. PLANT STATE: ${plantName} is ${plantVisual}. ${timePeriod} lighting. Chunky pixel soil near the pot. COMPOSITION: Side view. Plant on the left, ${petType} on the right. Both fully visible. BACKGROUND: Pure white — fully isolated sprite, no floor tile, no drop shadow. ABSOLUTE RULES: ZERO text, ZERO letters, ZERO numbers, ZERO words anywhere in the image.${eventElement}`;

    // Check for pet reference photo
    const safe = sanitizeEmail(user_email);
    const { data: refData } = await supabase.storage
      .from("pet-references")
      .download(`${safe}/${slot_id}/pet_reference.jpg`);

    if (refData) {
      const refB64 = btoa(String.fromCharCode(...new Uint8Array(await refData.arrayBuffer())));
      creativePrompt = `REFERENCE PHOTO: The attached photo shows the REAL pet this pixel art is based on. You MUST match its exact fur color, pattern, markings, eye color and distinctive features in the pixel art style. This is the #1 priority for character design. ` + creativePrompt;
    }

    const inputContent: any[] = [{ type: "input_text", text: creativePrompt }];

    if (refData) {
      const refB64 = btoa(String.fromCharCode(...new Uint8Array(await refData.arrayBuffer())));
      inputContent.push({ type: "input_image", image_url: `data:image/jpeg;base64,${refB64}` });
    }

    // Previous image for consistency
    const { data: prevImg } = await supabase.storage
      .from("pet-images")
      .download(`${safe}/${slot_id}/pet_current.png`);

    if (prevImg) {
      const prevB64 = btoa(String.fromCharCode(...new Uint8Array(await prevImg.arrayBuffer())));
      inputContent.push({ type: "input_image", image_url: `data:image/png;base64,${prevB64}` });
    }

    // Generate image via OpenAI
    const genBody: any = {
      model: "gpt-5.2",
      input: [{ role: "user", content: inputContent }],
      tools: [{ type: "image_generation", quality: "high", size: "1024x1024", output_format: "png", background: "transparent" }],
      store: true,
    };

    const previousResponseId = petState.last_response_id;
    if (previousResponseId) {
      genBody.previous_response_id = previousResponseId;
    }

    const imgResp = await fetch("https://api.openai.com/v1/responses", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${OPENAI_API_KEY}`,
      },
      body: JSON.stringify(genBody),
    });

    const imgData = await imgResp.json();

    let imageB64: string | null = null;
    let responseId = imgData.id;
    for (const output of imgData.output || []) {
      if (output.type === "image_generation_call") {
        imageB64 = output.result;
        break;
      }
    }

    if (imageB64) {
      // Decode base64 and upload to storage
      const binaryStr = atob(imageB64);
      const bytes = new Uint8Array(binaryStr.length);
      for (let i = 0; i < binaryStr.length; i++) {
        bytes[i] = binaryStr.charCodeAt(i);
      }

      await supabase.storage
        .from("pet-images")
        .upload(`${safe}/${slot_id}/pet_current.png`, bytes, {
          contentType: "image/png",
          upsert: true,
        });
    }

    // Generate phrases
    let caption = "";
    let phrases: string[] = [];
    try {
      const captionResp = await fetch("https://api.openai.com/v1/responses", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${OPENAI_API_KEY}`,
        },
        body: JSON.stringify({
          model: "gpt-4.1-mini",
          input: `Você é ${petName}, um ${petTypePt} com personalidade única cuidando de uma ${plantName}.\n${petConfig.type === "cat" ? "Personalidade: curioso, dramático, faz trocadilhos com plantas." : "Personalidade: leal, animado, adora dar conselhos de jardinagem."}\nEstado atual: temp ${temp}°C, umidade ${hum}%, solo ${soil}%, saúde ${healthScore}/100.\n${eventText ? "Evento de hoje: " + eventText : ""}\nGere EXATAMENTE 3 frases curtas e fofas (max 12 palavras cada) como se fosse ${petName} falando.\nCada frase em uma linha. Cada frase deve ter 1 emoji no início.\nTom: ${healthScore < 40 ? "preocupado e urgente" : healthScore > 80 ? "animado e orgulhoso" : "dedicado e cuidadoso"}.\nNão use aspas, markdown ou numeração.`,
        }),
      });
      const captionData = await captionResp.json();
      const rawCaption = (captionData.output_text || "").trim();
      phrases = rawCaption.split("\n").filter((l: string) => l.trim()).slice(0, 3);
      caption = phrases[0] || rawCaption;
    } catch {
      // Phrases are optional
    }

    // Update pet_state
    const newState = {
      last_response_id: responseId,
      last_prompt: creativePrompt.substring(0, 500),
      generated_at: new Date().toISOString(),
      event_of_day: eventText,
      pet_caption: caption,
      pet_phrases: phrases,
      health_score: healthScore,
      sensor_data: { temperature: temp, humidity: hum, soil: soil },
    };

    await supabase
      .from("plant_slots")
      .update({ pet_state: newState })
      .eq("user_email", user_email)
      .eq("slot_id", slot_id);

    // Mark job as done
    await supabase
      .from("pet_jobs")
      .update({ status: "done", completed_at: new Date().toISOString() })
      .eq("id", jobId);

    return new Response(
      JSON.stringify({ ok: true, job_id: jobId, status: "done" }),
      { headers: { "Content-Type": "application/json" } }
    );
  } catch (error: any) {
    // Mark job as error if possible
    try {
      const payload = await req.clone().json();
      await supabase
        .from("pet_jobs")
        .update({ status: "error", error_message: error.message })
        .eq("id", payload.record.id);
    } catch {
      // Best effort
    }

    return new Response(
      JSON.stringify({ ok: false, error: error.message }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
});
