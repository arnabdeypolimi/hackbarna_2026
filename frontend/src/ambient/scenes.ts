import type { CommandArgsByVerb } from '@contracts/protocol';
import text from './scenes.json';

/**
 * The scene ids are the backend's (src/tv_avatar/ambient/scenes.py), read through the generated
 * contract: the model can only ask for one of these, and a scene added there without a prompt
 * here fails this file's type check rather than arriving as an unknown id at runtime.
 */
export type SceneId = CommandArgsByVerb['show_ambient']['scene'];

export interface Scene {
  id: SceneId;
  /** The title shown over the picture, and what the avatar's failure note calls it. */
  label: string;
  /** Director's `prompt`: the whole scene, picture and sound, as one description. */
  prompt: string;
  /** Director's `script` beats, in order; configureFor() spaces them over the recording. */
  beats: string[];
}

/**
 * The words live in scenes.json, verbatim from the brief (relaxing-video-prompts.md), because two
 * things read them: this file, for the Director session, and tools/make_ambient_starters.py, for
 * the starter clip that plays while that session is being made. Each prompt names its sound as
 * well as its picture: Director makes the audio in the same pass as the video and puts it on its
 * own track, which the agent records alongside. Should a session come back silent, the README
 * says how to pin a recording with `audio_url` instead.
 *
 * `satisfies`, not a cast: a scene the backend can ask for with no entry in the JSON does not compile.
 */
const TEXT = text satisfies Record<SceneId, Omit<Scene, 'id'>>;

export const SCENES = Object.fromEntries(
  (Object.keys(TEXT) as SceneId[]).map((id) => [id, { id, ...TEXT[id] }]),
) as Record<SceneId, Scene>;

export const isScene = (id: string): id is SceneId => Object.prototype.hasOwnProperty.call(SCENES, id);

/**
 * Director's `configure` message for a scene. The brief's settings, one locked session: 1080p,
 * 16:9, its best audio, and the model keeping 12 segments of context so the scene stays the
 * same scene. The beats are spaced evenly over the recording rather than kept at the brief's
 * offsets (30, 60, 90 s…), so every one of them lands inside the clip that is kept.
 */
export function configureFor(scene: SceneId, seconds: number): object {
  const s = SCENES[scene];
  return {
    type: 'configure',
    protocol_version: 1,
    prompt_version: 1,
    resolution: '1080p',
    aspect_ratio: '16:9',
    audio_bitrate: 192000,
    memory: 12,
    prompt: s.prompt,
    script: s.beats.map((prompt, i) => ({ offset: Math.round((i * seconds) / s.beats.length), prompt })),
  };
}
