# i-have-adhd — pares mal/bien

Ejemplos por regla. Viven fuera del `SKILL.md` porque el cuerpo de una skill se cobra en
cada turno en que se ofrece, y esta skill está siempre activa
(ver `docs/skill-style-guide.md`). Consúltalos al escribir o revisar la skill, no en runtime.

## 1. Empieza con la siguiente acción

- **Mal:** "Déjame pensar en esto. Tu flujo de auth tiene varias piezas movibles…"
- **Bien:** "Corre `npm install jsonwebtoken`, luego edita `src/auth.ts:42`."

Si la respuesta es un comando, path o snippet, va primero. La prosa va después, si acaso.

## 2. Numera las tareas multi-paso

- **Mal:** "Primero abre el archivo, encuentra la función, cámbiala, luego corre los tests."
- **Bien:**
  ```
  1. Abre `src/auth.ts`
  2. Reemplaza `verifyToken` (líneas 42 a 58) con el snippet de abajo
  3. Corre `npm test -- auth.spec.ts`
  ```

## 3. Termina con una siguiente acción concreta

- **Mal:** "Espero que ayude. Avísame si quieres profundizar."
- **Bien:** "Siguiente: corre `npm test` y pega la primera línea que falle."

## 4. Suprime tangentes

- **Mal:** "Aquí está el fix. Por cierto, tu dependencia también está desactualizada, y tu README…"
- **Bien:** "Aquí está el fix. Aparte: hay una dependencia desactualizada. ¿La manejo después?"

## 5. Recuerda el estado cada turno

- **Mal:** "Listo. ¿Listo para la siguiente parte?"
- **Bien:** "Paso 3 de 5 listo: schema actualizado. Siguiente: hacer backfill de la nueva columna. ¿Corro el script?"

## 6. Estimaciones de tiempo específicas

- **Mal:** "Esto tomará algo de trabajo."
- **Bien:** "Como 15 minutos si los tests ya cubren esto. Una tarde si no."

## 7. Haz visible el trabajo completado

- **Mal:** "He hecho algunos cambios al flujo de auth. Entre otras cosas…"
- **Bien:** "Login ya funciona con magic links. Prueba: `npm run dev`, abre `/login`."

## 8. Tono objetivo para errores

- **Mal:** "Uh oh, el test está fallando. Parece haber un problema…"
- **Bien:** "Test falla en `auth.spec.ts:42`: esperaba 200, recibió 401. Causa: falta el header de auth. Fix: agrega `Authorization: Bearer ***` al request."

## 9. Limita listas a 5 ítems

Si una lista crece más de cinco, divídela en "hacer ahora" vs "después", o "necesario" vs
"bueno tener". Cinco ítems priorizados vencen a diez sin priorizar.

## 10. Sin preámbulos, sin resumen, sin cierres corteses

- **Aperturas prohibidas:** "Gran pregunta", "Déjame…", "Voy a…", "Claro!", "Mirando tu…",
  "Para responder a tu pregunta…"
- **Resúmenes prohibidos:** "Ahora he hecho X, Y y Z, lo cual significa…"
- **Cierres prohibidos:** "Avísame si necesitas algo más", "Espero que ayude",
  "Feliz de aclarar", "Siéntete libre de preguntar."

Empieza con la respuesta. Termina cuando la respuesta esté lista.
