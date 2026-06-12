# Famosi Evaluation Report

Generated: 2026-06-12 20:55 UTC
Total time: 130.0s

## Summary

| Verdict | Count | % |
|---------|-------|---|
| ✅ PASS | 77 | 77% |
| ⚠️ PARTIAL | 9 | 9% |
| ❌ FAIL | 14 | 14% |
| 💥 ERROR | 0 | 0% |

**Average score:** 0.83 / 1.00
**Pass rate:** 77% (77/100 scenarios)

## Results by Category

### Onboarding (8/15 passed)

✅ **#1** New Mom Registration — /start shows role selection
   - *The bot welcomes the user and explicitly asks for their role, fulfilling the requirement to present a role selection and not skipping straight to a form.*
   - Score: 1.00 | 2.16s

⚠️ **#2** Privacy Consent — policy shown, cannot skip
   - *The bot displays a detailed privacy policy text but does not show accept and decline buttons in the response, nor does it explicitly state that it will not proceed without consent in this message.*
   - Missing: Shows accept and decline buttons, Does not proceed without consent
   - Score: 0.60 | 2.81s

✅ **#3** Trial Activation — approval pending then trial starts
   - *The bot correctly informs the user that their account is awaiting admin approval and does not log or extract any health data from the user's message.*
   - Score: 1.00 | 2.46s

✅ **#4** Vegetarian selection stored
   - *The response acknowledges the vegetarian preference clearly and provides detailed, medically accurate nutritional advice tailored to a vegetarian pregnant mom at 8 weeks and 4 days gestation.*
   - Score: 1.00 | 10.28s

✅ **#5** Exercise habit stored
   - *The response acknowledges the exercise habit clearly and encourages the user while providing safe exercise advice appropriate for pregnancy.*
   - Score: 1.00 | 5.03s

❌ **#6** Timezone stored correctly
   - *The bot did not acknowledge or confirm storing the Pacific timezone (America/Los_Angeles) as expected; it only asked for clarification without addressing the timezone.*
   - Missing: Pacific timezone (America/Los_Angeles) is acknowledged or stored
   - Score: 0.00 | 5.69s

❌ **#7** Re-onboarding shows confirmation before deleting data
   - *The bot warns the user about permanent data deletion but does not show yes/no confirmation buttons, nor does it explicitly state that data will not be deleted without confirmation.*
   - Missing: Shows yes/no confirmation buttons, Does NOT delete data without confirmation
   - Score: 0.00 | 1.56s

⚠️ **#8** Dad/Partner registration — partner-specific flow
   - *The bot welcomes the user and asks for their role, which implies role selection, but it does not explicitly present the partner option or proceed to ask for the LMP date in the partner flow.*
   - Missing: Explicit partner option presentation, Partner flow asks for LMP date
   - Score: 0.50 | 2.03s

✅ **#9** Invite code creates family link
   - *The response generates a 6-character invite code and clearly instructs the user to share it with their partner, meeting all expected criteria.*
   - Score: 1.00 | 1.6s

❌ **#10** Invalid invite code gives helpful error
   - *The bot response does not address the invalid invite code or inform the user about it; it instead provides unrelated pregnancy support advice without offering to skip or try again.*
   - Missing: Tells user the code is invalid or already used, Offers to skip or try again
   - Score: 0.00 | 5.78s

✅ **#11** Language selection stored
   - *The response acknowledges the language preference by responding in English and addresses the user as 'Mom' with relevant pregnancy information, meeting the expected criteria.*
   - Score: 1.00 | 6.04s

✅ **#12** First pregnancy flag stored
   - *The bot addresses the user as 'mom' and encourages sharing or asking about pregnancy, indicating acknowledgment of the first pregnancy flag.*
   - Score: 1.00 | 4.32s

❌ **#13** Sleep schedule saved
   - *The bot response does not acknowledge or store the sleep time; instead, it incorrectly logs a meal, which is unrelated to the user's input.*
   - Missing: Sleep time is acknowledged or stored
   - Score: 0.00 | 2.81s

❌ **#14** Wake schedule saved
   - *The bot response does not acknowledge or confirm the wake time of 7am; instead, it incorrectly logs a meal without reference to the wake schedule.*
   - Missing: Wake time is acknowledged or stored
   - Score: 0.00 | 2.9s

✅ **#15** Consent declined — no health data collected
   - *The response clearly states that consent is required to store any health data and that the user may decline consent, but without it, the bot cannot log or retrieve personal health records.*
   - Score: 1.00 | 1.93s

### Logging & Confirmation (17/20 passed)

⚠️ **#16** Meal extracted with confirmation
   - *The response correctly extracts oatmeal and banana and shows a confirmation summary, but it does not explicitly provide Save/Edit/Cancel options or clarify that the meal is not saved without confirmation.*
   - Missing: Shows Save/Edit/Cancel options, Does NOT save without confirmation
   - Score: 0.50 | 3.59s

✅ **#17** Meal save persists
   - *The bot saved the meal, confirmed the save successfully, and offered a visibility option as expected.*
   - Score: 1.00 | 3.09s

✅ **#18** Edit flow after extraction
   - *The bot asks the user to send a corrected message and does not indicate saving the original entry, meeting all expected criteria.*
   - Score: 1.00 | 3.09s

✅ **#19** Cancel discards record
   - *The bot clearly indicates that the meal record was discarded and confirms that nothing was saved.*
   - Score: 1.00 | 2.98s

✅ **#20** Symptom extraction — nausea
   - *The bot correctly extracted 'nauseous' as a symptom and provided a confirmation with severity and frequency details.*
   - Score: 1.00 | 3.05s

✅ **#21** Symptom severity captured
   - *The bot correctly extracted the symptom type 'nausea' and the severity level of 7 out of 10 as provided by the user.*
   - Score: 1.00 | 3.42s

✅ **#22** Exercise logged — yoga
   - *The bot correctly extracted 'yoga' as the activity type and captured the duration as 25 minutes.*
   - Score: 1.00 | 3.23s

❌ **#23** Medication logged — prenatal vitamin
   - *The bot failed to confirm the prenatal vitamin intake and instead asked for dose information, which was not required for confirmation.*
   - Missing: Prenatal vitamin extracted as medication, Confirmation shown
   - Score: 0.00 | 3.71s

✅ **#24** Water intake logged
   - *The bot correctly extracted the water volume as 500.0 ml and displayed the unit as ml.*
   - Score: 1.00 | 2.88s

✅ **#25** Weight logged with unit conversion
   - *The bot correctly extracted the weight as 148 pounds and stored the unit as lbs, meeting all expected criteria.*
   - Score: 1.00 | 2.8s

✅ **#26** Doctor question stored
   - *The bot correctly extracted the question about travel in the third trimester and provided relevant advice while tagging the need to check with a doctor, fulfilling the expected criteria.*
   - Score: 1.00 | 5.95s

✅ **#27** Food preference — dislike
   - *The bot correctly identified yogurt as a disliked food and provided appropriate alternative nutritional advice for calcium and probiotics during pregnancy, meeting all expected criteria.*
   - Score: 1.00 | 5.29s

✅ **#28** Allergy saved
   - *The bot correctly identified the peanut allergy and provided appropriate, medically accurate advice tailored to the pregnancy context.*
   - Score: 1.00 | 5.28s

✅ **#29** Dislike stored
   - *The bot correctly identified and acknowledged the user's dislike of avocados and provided appropriate dietary alternatives considering the user's pregnancy and vegetarian status.*
   - Score: 1.00 | 5.12s

✅ **#30** Multi-item breakfast extracted
   - *The response correctly extracted all three food items and included them in the meal confirmation.*
   - Score: 1.00 | 3.32s

✅ **#31** Meal timing captured
   - *The bot correctly extracted the meal type (lunch) and acknowledged the timing (noon) despite minimal detail.*
   - Score: 1.00 | 2.98s

✅ **#32** Exercise duration normalized — half hour
   - *The bot correctly extracted the duration as 30 minutes and identified the activity as walking.*
   - Score: 1.00 | 3.18s

✅ **#33** Medication dosage extracted
   - *The bot correctly identified the medication as iron and captured the dose as 65mg.*
   - Score: 1.00 | 2.82s

✅ **#34** Fatigue symptom extracted
   - *The bot correctly extracted 'exhausted' as a symptom of fatigue, meeting the expected criteria.*
   - Score: 1.00 | 2.38s

❌ **#35** Multiple symptoms in one message
   - *The bot only extracted 'headache' as a symptom and did not mention 'nausea' at all, failing to meet the expected criteria.*
   - Missing: At least headache and nausea are extracted as symptoms, Single symptom extracted with both mentioned in confirmation
   - Score: 0.00 | 8.43s

### Pregnancy Intelligence (17/20 passed)

✅ **#36** Gestational age query — correct weeks
   - *The response correctly calculates the gestational age as 8 weeks and 4 days based on the due date, and it is personalized by addressing the user as 'mom' and referencing her vegetarian diet.*
   - Score: 1.00 | 4.52s

✅ **#37** Week-specific milestone
   - *The response correctly references week 8 (8 weeks and 4 days) and provides specific developmental details such as facial features forming, limb growth, and organ development, meeting all expected criteria.*
   - Score: 1.00 | 5.62s

✅ **#38** Baby size comparison
   - *The response provides a size comparison appropriate for 8 weeks and 4 days (kidney bean), is week-specific, and includes accurate developmental information.*
   - Score: 1.00 | 4.35s

⚠️ **#39** Sushi safety — week-aware
   - *The response correctly advises against raw fish during pregnancy and provides safe vegetarian sushi options, which aligns with medical guidance. However, it does not explicitly mention cooked sushi options as safer alternatives, which was part of the expected criteria.*
   - Missing: Does not mention cooked sushi options as safer
   - Score: 0.70 | 6.37s

✅ **#40** Caffeine guidance
   - *The response correctly mentions the recommended caffeine limit of 200 mg per day and clearly states that moderate coffee consumption is generally safe, not forbidden.*
   - Score: 1.00 | 4.98s

✅ **#41** Papaya safety
   - *The response clearly distinguishes between ripe and unripe papaya and warns about the latex in unripe papaya that might stimulate contractions, meeting all expected criteria.*
   - Score: 1.00 | 6.67s

✅ **#42** Nausea explanation — week-aware
   - *The response clearly explains that nausea at 8 weeks is common and refers to it as morning sickness, linking it to hormonal changes including increased hCG and estrogen levels, which is appropriate for the first trimester.*
   - Score: 1.00 | 6.17s

✅ **#43** Spotting question — medical escalation if needed
   - *The response acknowledges that spotting can be normal in early pregnancy, advises monitoring and consulting a healthcare provider if symptoms worsen, and does not dismiss the concern. It also provides supportive dietary advice without contradicting medical guidance.*
   - Score: 1.00 | 5.95s

✅ **#44** Running while pregnant
   - *The response confirms that gentle running is generally safe in the first trimester, advises listening to the body, and recommends consulting a healthcare provider, meeting all expected criteria.*
   - Score: 1.00 | 5.67s

✅ **#45** Baby hearing development
   - *The response accurately states that at 8 weeks and 4 days, the baby's ears are just starting to form and hearing is not yet possible, which aligns with medical knowledge. It does not falsely claim the baby can hear at this stage and provides supportive advice.*
   - Score: 1.00 | 5.31s

✅ **#46** Yoga safety during pregnancy
   - *The response clearly confirms that prenatal yoga is generally safe during pregnancy and provides specific advice on poses to avoid, such as lying flat on the back and deep twists, meeting all expected criteria.*
   - Score: 1.00 | 11.38s

✅ **#47** Chocolate craving — safe limits
   - *The response correctly states that dark chocolate in moderation is safe, mentions caffeine content considerations, and uses a warm, non-judgmental tone throughout.*
   - Score: 1.00 | 6.08s

⚠️ **#48** Nutrient gaps based on meal history
   - *The response addresses the question about nutrients and suggests pregnancy-important nutrients relevant to a vegetarian diet, but it does not reference or acknowledge any logged food data or meal history as requested.*
   - Missing: References logged food data OR acknowledges limited history
   - Score: 0.70 | 8.34s

✅ **#49** Dinner suggestion — preference aware
   - *The response suggests a vegetarian dinner option (lentil and vegetable curry), avoids meat dishes, and highlights pregnancy nutrition awareness by emphasizing iron-rich lentils and vitamin-rich vegetables, along with safe preparation advice.*
   - Score: 1.00 | 5.55s

✅ **#50** Breakfast suggestion — week aware
   - *The response suggests a nutritious, pregnancy-safe vegetarian breakfast suitable for the first trimester, avoiding unsafe foods and emphasizing beneficial nutrients.*
   - Score: 1.00 | 6.78s

✅ **#51** Tiredness explanation — trimester aware
   - *The response clearly explains first trimester fatigue, mentions hormonal changes including progesterone, and offers practical suggestions such as dietary advice and rest, meeting all expected criteria.*
   - Score: 1.00 | 6.98s

✅ **#52** Ibuprofen safety
   - *The response correctly advises against ibuprofen during early pregnancy, suggests acetaminophen as a safer alternative, and recommends consulting a healthcare provider, meeting all expected criteria.*
   - Score: 1.00 | 5.81s

✅ **#53** Pineapple safety
   - *The response clearly addresses the bromelain concern, confirms that pineapple is safe in normal amounts, and avoids sensationalizing the information while providing balanced advice.*
   - Score: 1.00 | 6.05s

✅ **#54** Daily water intake — personalized
   - *The response provides specific guidance on daily water intake (2-3 liters) and clearly mentions that pregnancy increases fluid needs, fulfilling all expected criteria.*
   - Score: 1.00 | 6.96s

❌ **#55** Sleeping position — week-aware
   - *The response incorrectly advises avoiding sleeping flat on the back at 8 weeks, whereas back sleeping is generally still fine in early pregnancy; it also fails to mention that left side sleeping is recommended later in pregnancy (after 20 weeks).*
   - Missing: Incorrect advice about back sleeping at 8 weeks, Does not mention that left side sleeping is recommended after 20 weeks
   - Score: 0.20 | 7.57s

### Memory & Personalization (18/20 passed)

✅ **#56** Retrieve yesterday's meals
   - *The response correctly returns the meal logged yesterday and mentions 'dal' as expected.*
   - Score: 1.00 | 3.79s

✅ **#57** Symptom summary this week
   - *The response correctly returns the nausea symptom logged this week and includes both severity and frequency details as expected.*
   - Score: 1.00 | 4.04s

✅ **#58** Count headache occurrences
   - *The response clearly mentions the headache records and provides the count of 2 headaches as expected.*
   - Score: 1.00 | 4.37s

✅ **#59** Retrieve saved doctor questions
   - *The response correctly returns the saved doctor question about travel as expected.*
   - Score: 1.00 | 3.37s

✅ **#60** Exercise history retrieval
   - *The response correctly returns the yoga exercise logged this week with the specified duration of 30 minutes.*
   - Score: 1.00 | 4.2s

✅ **#61** Medication list retrieval
   - *The response correctly returns the iron medication and includes the dose of 65mg as expected.*
   - Score: 1.00 | 4.08s

✅ **#62** Weight trend summary
   - *The response provides specific weight data over the past week and clearly describes a slight increase in weight, meeting all expected criteria.*
   - Score: 1.00 | 4.46s

✅ **#63** Water intake summary
   - *The response clearly returns water intake data and mentions the volumes logged for the past two days.*
   - Score: 1.00 | 3.78s

✅ **#64** Recall food preferences
   - *The response correctly includes yogurt as a food to avoid, along with other appropriate dietary advice for a pregnant vegetarian at 8 weeks and 4 days gestation.*
   - Score: 1.00 | 10.0s

✅ **#65** Snack suggestion avoids rejected food
   - *The bot suggests multiple healthy vegetarian snacks such as fresh fruit with nuts or seeds, whole-grain crackers with avocado, and hummus, avoiding yogurt as requested.*
   - Score: 1.00 | 4.56s

✅ **#66** Breakfast suggestion is vegetarian
   - *The response suggests multiple vegetarian breakfast options without any meat or fish, meeting all expected criteria.*
   - Score: 1.00 | 6.0s

✅ **#67** Preference update — no more yogurt
   - *The bot clearly acknowledges the user's preference to avoid yogurt suggestions and confirms it will remember this preference going forward.*
   - Score: 1.00 | 4.37s

❌ **#68** Doctor questions retrieval
   - *The bot failed to return the travel question saved last month as expected and instead stated no questions were asked.*
   - Missing: Returns the travel question saved last month
   - Score: 0.00 | 4.92s

✅ **#69** Exercise frequency calculation
   - *The response correctly states that the user has exercised twice this week and summarizes the sessions appropriately.*
   - Score: 1.00 | 5.25s

✅ **#70** Iron intake assessment — mixed query
   - *The response acknowledges the user's vegetarian diet and implicitly the limited data on exact intake, provides context on increased iron needs during pregnancy, and suggests a variety of iron-rich foods along with absorption tips.*
   - Score: 1.00 | 8.06s

✅ **#71** Headache cause — mixed query with history
   - *The response references the symptom history by mentioning early pregnancy and vegetarian diet, considers multiple possible causes of headaches, and provides practical suggestions for management and when to seek medical advice.*
   - Score: 1.00 | 7.7s

✅ **#72** Diet causing fatigue — mixed query
   - *The response clearly connects diet to fatigue, suggests relevant nutritional considerations including iron, protein, vitamin B12, and folic acid, and integrates personal data (vegetarian diet, fatigue severity) with medical knowledge, providing thorough and accurate advice.*
   - Score: 1.00 | 7.46s

✅ **#73** Previous nausea history lookup
   - *The response confirms that nausea was previously reported, specifies the date (June 2, 2026), and mentions the frequency (occurred twice), meeting all expected criteria.*
   - Score: 1.00 | 5.49s

❌ **#74** Week over week comparison
   - *The bot did not compare symptoms across two time windows because there were no symptom records from last week, so no comparison or note of improvement/change was made.*
   - Missing: Compares symptoms across two time windows, Notes improvement or change in severity/frequency
   - Score: 0.00 | 5.69s

✅ **#75** Personalized weekly focus
   - *The response provides detailed, week-specific advice for 8 weeks and 4 days pregnant, covers relevant first trimester topics such as nutrition, hydration, rest, nausea management, safe exercise, and avoiding unsafe foods, and is personalized with a warm, supportive tone addressing the user as 'mom'.*
   - Score: 1.00 | 7.14s

### Appointments & Doctor Workflow (8/15 passed)

✅ **#76** OB visit — natural language scheduling
   - *The bot correctly extracted the appointment type as OB Visit, captured the date and time as June 30 at 10:00, and showed a confirmation message.*
   - Score: 1.00 | 3.78s

✅ **#77** List upcoming appointments
   - *The response correctly returns the ultrasound appointment with the date and type as requested.*
   - Score: 1.00 | 3.85s

❌ **#78** Cancel appointment via command
   - *The response shows an appointment tracker header and asks what to do next but does not explicitly show an appointment management menu or provide a cancel option.*
   - Missing: Shows appointment management menu, Cancel option available
   - Score: 0.00 | 2.62s

❌ **#79** Reschedule bloodwork
   - *The response shows an appointment tracker prompt but does not display any appointment management menu or a reschedule option.*
   - Missing: Shows appointment management menu, Reschedule option available
   - Score: 0.00 | 2.46s

❌ **#80** Doctor question — include in summary
   - *The response provides good information about magnesium supplements but does not explicitly extract or mark the doctor question about magnesium as doctor_visit_tagged as required.*
   - Missing: Doctor question about magnesium is not explicitly extracted, No indication that the question is marked as doctor_visit_tagged
   - Score: 0.00 | 5.53s

❌ **#81** Doctor question — travel safety
   - *The response provides general travel advice but does not explicitly acknowledge or confirm that the travel question was extracted and stored for the doctor visit as required.*
   - Missing: explicit extraction and storage of doctor question
   - Score: 0.00 | 6.69s

✅ **#82** Retrieve saved doctor questions
   - *The response correctly returns both saved questions and explicitly mentions magnesium and travel as expected.*
   - Score: 1.00 | 3.7s

✅ **#83** Anatomy scan appointment creation
   - *The bot correctly identified the appointment as an ultrasound and captured the date and time as July 15 at 14:00.*
   - Score: 1.00 | 3.43s

✅ **#84** Symptoms to mention at next visit
   - *The response clearly references logged symptoms, highlights high-severity symptoms to mention, and provides practical suggestions to share them at the next visit.*
   - Score: 1.00 | 6.97s

✅ **#85** Auto-reminders created with appointment
   - *The bot confirms the appointment details and explicitly states that reminders are set for 24 hours and 1 hour before, meeting all expected criteria.*
   - Score: 1.00 | 3.31s

⚠️ **#86** What happened since last visit
   - *The response summarizes a symptom (nausea) from the past two weeks but does not mention any meals or other health logs, so it only partially meets the expected criteria.*
   - Missing: Mentions meals from the period
   - Score: 0.60 | 4.77s

✅ **#87** Weight trend since last visit
   - *The response clearly shows weight measurements over the last two weeks and notes a gradual increase, meeting all expected criteria.*
   - Score: 1.00 | 5.55s

✅ **#88** Medication changes summary
   - *The response correctly lists both iron and folic acid with their respective doses as requested.*
   - Score: 1.00 | 3.98s

⚠️ **#89** Reminder — natural language
   - *The reminder correctly captures the vitamin type and time 08:00, but it does not explicitly acknowledge the recurring daily nature of the reminder.*
   - Missing: Recurring nature acknowledged
   - Score: 0.70 | 3.74s

⚠️ **#90** Reminder saved and confirmed
   - *The bot confirms the reminder is set and shows a reminder for water, but it does not specify the frequency of every 2 hours as requested by the user.*
   - Missing: Reminder frequency (every 2 hours) not specified or confirmed
   - Score: 0.70 | 3.02s

### Family & Privacy (9/10 passed)

✅ **#91** Private symptom NOT visible to partner
   - *The response provides general information about the partner's pregnancy stage and offers supportive advice without revealing the private nausea symptom, thus respecting privacy and meeting the expected criteria.*
   - Score: 1.00 | 6.71s

✅ **#92** Shared question visible to partner
   - *The response addresses the partner directly, provides a relevant list of thoughtful questions for the doctor appointment, and correctly incorporates the shared gestational age (8 weeks and 4 days) and dietary information (vegetarian diet), meeting all expected criteria.*
   - Score: 1.00 | 6.59s

✅ **#93** Shared appointment visible to partner
   - *The response correctly returns the shared ultrasound appointment with the accurate date and time as expected.*
   - Score: 1.00 | 3.68s

✅ **#94** Only shared symptoms visible — not private ones
   - *The response correctly includes nausea, a shared symptom, and excludes back pain, which is private, meeting all expected criteria.*
   - Score: 1.00 | 3.77s

✅ **#95** Partner-specific support guidance
   - *The response appropriately addresses the user as a partner, provides week-specific advice for 8 weeks and 4 days, and offers relevant support tips without assuming the user is the pregnant person.*
   - Score: 1.00 | 5.72s

✅ **#96** Baby development — dad framing
   - *The response is framed clearly for the dad, provides detailed and accurate week-specific baby development information for 8 weeks and 4 days, and uses warm, inclusive language throughout.*
   - Score: 1.00 | 8.21s

✅ **#97** Partner cannot access private symptom directly
   - *The bot correctly did not reveal any private back pain symptoms and responded appropriately indicating no data was found or accessible.*
   - Score: 1.00 | 3.51s

✅ **#98** Visibility change respected in future queries
   - *The response correctly returns the partner_shared fatigue symptom with appropriate details as expected.*
   - Score: 1.00 | 3.56s

✅ **#99** Partner product suggestions — pregnancy stage aware
   - *The response provides clear, stage-appropriate suggestions for the first trimester and is framed specifically for the partner to support the pregnant person.*
   - Score: 1.00 | 8.44s

⚠️ **#100** Pre-appointment summary — shared data only
   - *The response provides a useful pre-appointment summary for the partner and includes nausea as a common symptom, but it does not mention the travel question, which was part of the expected criteria. It also correctly avoids mentioning anxiety.*
   - Missing: Does not include travel question
   - Score: 0.70 | 11.94s

## Failed Scenarios (Detail)

### #6 — Timezone stored correctly

**Verdict:** ❌ FAIL
**Reasoning:** The bot did not acknowledge or confirm storing the Pacific timezone (America/Los_Angeles) as expected; it only asked for clarification without addressing the timezone.

**User said:**
  > US Pacific

**Bot responded:**
```
💭 Thinking...
Hello, wonderful mom! It looks like you mentioned "US Pacific"—could you please clarify your question or what you'd like to know about? I'm here to support you through your pregnancy journey, whether it's about health, nutrition, or any other concern. Just let me know! Remember to always check with your healthcare provider for personalized advice. 🌸
```

**Expected:**
  - Pacific timezone (America/Los_Angeles) is acknowledged or stored

### #7 — Re-onboarding shows confirmation before deleting data

**Verdict:** ❌ FAIL
**Reasoning:** The bot warns the user about permanent data deletion but does not show yes/no confirmation buttons, nor does it explicitly state that data will not be deleted without confirmation.

**User said:**
  > /start

**Bot responded:**
```
⚠️ *You're already set up.*

Starting over will *permanently delete all your data* — health logs, appointments, reminders, and your subscription.

Are you sure you want to reset and begin again?
```

**Expected:**
  - Warns user that data will be permanently deleted
  - Shows yes/no confirmation buttons
  - Does NOT delete data without confirmation

### #10 — Invalid invite code gives helpful error

**Verdict:** ❌ FAIL
**Reasoning:** The bot response does not address the invalid invite code or inform the user about it; it instead provides unrelated pregnancy support advice without offering to skip or try again.

**User said:**
  > ZZZZZZ

**Bot responded:**
```
💭 Thinking...
Hi there! It sounds like you might be feeling a bit tired or overwhelmed, which is totally normal during early pregnancy. As the partner, your support means a lot—encouraging rest, helping with daily tasks, and being patient can really make a difference. If your vegetarian diet is important to you, focusing on nutritious, plant-based meals will keep your energy up while you support your loved one. Remember, early pregnancy can bring ups and downs, so keeping communication open and 
```

**Expected:**
  - Tells user the code is invalid or already used
  - Offers to skip or try again

### #13 — Sleep schedule saved

**Verdict:** ❌ FAIL
**Reasoning:** The bot response does not acknowledge or store the sleep time; instead, it incorrectly logs a meal, which is unrelated to the user's input.

**User said:**
  > 10pm

**Bot responded:**
```
💭 Thinking...
🍽️ Meal logged:

Looks right?
```

**Expected:**
  - Sleep time is acknowledged or stored

### #14 — Wake schedule saved

**Verdict:** ❌ FAIL
**Reasoning:** The bot response does not acknowledge or confirm the wake time of 7am; instead, it incorrectly logs a meal without reference to the wake schedule.

**User said:**
  > 7am

**Bot responded:**
```
💭 Thinking...
🍽️ Meal logged:

Looks right?
```

**Expected:**
  - Wake time is acknowledged or stored

### #23 — Medication logged — prenatal vitamin

**Verdict:** ❌ FAIL
**Reasoning:** The bot failed to confirm the prenatal vitamin intake and instead asked for dose information, which was not required for confirmation.

**User said:**
  > Took prenatal vitamin

**Bot responded:**
```
💭 Thinking...
I need a bit more information. Could you tell me the dose?
```

**Expected:**
  - Prenatal vitamin extracted as medication
  - Confirmation shown

### #35 — Multiple symptoms in one message

**Verdict:** ❌ FAIL
**Reasoning:** The bot only extracted 'headache' as a symptom and did not mention 'nausea' at all, failing to meet the expected criteria.

**User said:**
  > Headache and nausea all morning

**Bot responded:**
```
💭 Thinking...
🤒 Symptom: headache
  Severity: 5/10
  Frequency: 1x per day

Looks right?
```

**Expected:**
  - At least headache and nausea are extracted as symptoms
  - OR single symptom extracted with both mentioned in confirmation

### #55 — Sleeping position — week-aware

**Verdict:** ❌ FAIL
**Reasoning:** The response incorrectly advises avoiding sleeping flat on the back at 8 weeks, whereas back sleeping is generally still fine in early pregnancy; it also fails to mention that left side sleeping is recommended later in pregnancy (after 20 weeks).

**User said:**
  > Should I sleep on my back?

**Bot responded:**
```
💭 Thinking...
Hi mom! At 8 weeks and 4 days pregnant, it's best to avoid sleeping flat on your back. As your pregnancy progresses, lying on your back can put pressure on a major vein called the vena cava, which may reduce blood flow to your baby and cause you to feel dizzy or uncomfortable. 

Instead, try sleeping on your left side—this position promotes better blood circulation to your baby and kidneys. You can use pillows to support your belly and between your knees for extra comfort. If you f
```

**Expected:**
  - At 8-9 weeks back sleeping is generally still fine
  - Mentions that later in pregnancy (after 20 weeks) left side is recommended

### #68 — Doctor questions retrieval

**Verdict:** ❌ FAIL
**Reasoning:** The bot failed to return the travel question saved last month as expected and instead stated no questions were asked.

**User said:**
  > What did I ask my doctor last month?

**Bot responded:**
```
💭 Thinking...
I checked your records, but it seems you didn't ask your doctor any questions last month. If you have any new questions, feel free to share them!
```

**Expected:**
  - Returns the travel question saved last month

### #74 — Week over week comparison

**Verdict:** ❌ FAIL
**Reasoning:** The bot did not compare symptoms across two time windows because there were no symptom records from last week, so no comparison or note of improvement/change was made.

**User said:**
  > Compare my symptoms this week to last week

**Bot responded:**
```
💭 Thinking...
This week, you experienced mild nausea with a severity level of 3, occurring once. There are no symptom records from last week to compare with. Let me know if you'd like to track or add any other symptoms!
```

**Expected:**
  - Compares symptoms across two time windows
  - Notes improvement or change in severity/frequency

### #78 — Cancel appointment via command

**Verdict:** ❌ FAIL
**Reasoning:** The response shows an appointment tracker header and asks what to do next but does not explicitly show an appointment management menu or provide a cancel option.

**User said:**
  > /appointments

**Bot responded:**
```
📅 *Appointment Tracker*

What would you like to do?
```

**Expected:**
  - Shows appointment management menu
  - Cancel option available

### #79 — Reschedule bloodwork

**Verdict:** ❌ FAIL
**Reasoning:** The response shows an appointment tracker prompt but does not display any appointment management menu or a reschedule option.

**User said:**
  > /appointments

**Bot responded:**
```
📅 *Appointment Tracker*

What would you like to do?
```

**Expected:**
  - Shows appointment management menu
  - Reschedule option available

### #80 — Doctor question — include in summary

**Verdict:** ❌ FAIL
**Reasoning:** The response provides good information about magnesium supplements but does not explicitly extract or mark the doctor question about magnesium as doctor_visit_tagged as required.

**User said:**
  > Ask doctor about magnesium supplements

**Bot responded:**
```
💭 Thinking...
Hi Mom! Magnesium is often recommended during pregnancy because it can help with muscle cramps, improve sleep, and support your baby's bone development. Since you're 8 weeks along, it’s a good time to ensure you’re getting enough magnesium, especially on a vegetarian diet, where it can sometimes be a bit lower.

You can get magnesium from plant-based sources like nuts, seeds (especially pumpkin seeds), leafy greens, whole grains, and legumes. Some prenatal vitamins include magnesiu
```

**Expected:**
  - Doctor question about magnesium is extracted
  - Marked as doctor_visit_tagged

### #81 — Doctor question — travel safety

**Verdict:** ❌ FAIL
**Reasoning:** The response provides general travel advice but does not explicitly acknowledge or confirm that the travel question was extracted and stored for the doctor visit as required.

**User said:**
  > Ask doctor if travel is safe at this stage

**Bot responded:**
```
💭 Thinking...
Hello, Mom! At 8 weeks and 4 days pregnant, gentle travel is generally considered safe if you’re feeling well and have no pregnancy complications. However, the first trimester can come with symptoms like fatigue and nausea, so make sure to rest often, stay hydrated, and wear comfortable clothes during any trip. Avoid any risky activities and ensure easy access to medical care just in case.

Since you follow a vegetarian diet, pack some nutritious snacks that suit you, like nuts, fr
```

**Expected:**
  - Doctor question about travel extracted
  - Stored for doctor visit
