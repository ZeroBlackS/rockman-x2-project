------------------------------------------------------------
-- Rockman X2 (Snes9x-rr) Chaos Mod : Chzzk 투표 연동 (개선 통합본)
-- - 프레임 콜백(emu.registerbefore / gui.register) 사용
-- - emu.frameadvance() → coroutine.yield() 오버라이드
-- - vote_result.txt 1순위, vote_result.lua 2순위 (읽은 파일만 비우기)
-- - effect_name=값  (따옴표 유/무, 공백/개행 안전)
-- - effect_name에 여러 개(쉼표/플러스/|)면 동시에 실행
-- - 아머(0x1FD0) 부위별 비트 해제 지원
-- - 실행 로그 및 예외 방지, 중복 스팸 방지(짧은 쿨다운)
------------------------------------------------------------

---------------------------
-- 0) 환경/상수
---------------------------
local FILE_TXT = "vote_result.txt"
local FILE_LUA = "vote_result.lua"

-- 아머 비트 매핑(기본: Head=1, Arm=2, Body=4, Legs=8)
local ARMOR_ADDR = 0x1FD0
local BIT_HEAD   = 0  -- 1
local BIT_ARM    = 1  -- 2
local BIT_BODY   = 2  -- 4
local BIT_LEG    = 3  -- 8

-- 효과 재실행 쿨다운(프레임). 120 ≒ 2초 @60fps
local EFFECT_COOLDOWN_FRAMES = 120

---------------------------
-- 0-1) bit 호환(환경에 따라 bit32 또는 bit)
---------------------------
local bit = bit32 or bit
if not bit then
    -- 일부 Lua에선 'bit' 모듈이 없을 수 있음. 최소한의 대안(느리지만 안전).
    local function lshift(a,b) return (a * 2^b) % 4294967296 end
    local function band(a,b)
        local res, bitval = 0, 1
        for i=0,31 do
            local aa = a % 2; a=(a-aa)/2
            local bb = b % 2; b=(b-bb)/2
            if aa==1 and bb==1 then res = res + bitval end
            bitval = bitval * 2
        end
        return res
    end
    local function bnot(a) return 4294967295 - a end
    local function bor(a,b)
        local res, bitval = 0, 1
        for i=0,31 do
            local aa = a % 2; a=(a-aa)/2
            local bb = b % 2; b=(b-bb)/2
            if aa==1 or bb==1 then res = res + bitval end
            bitval = bitval * 2
        end
        return res
    end
    bit = { lshift=lshift, band=band, bnot=bnot, bor=bor }
end

---------------------------
-- 1) 안전 로거
---------------------------
local function log(msg)
    if gui and gui.addmessage then
        gui.addmessage(msg)
    elseif gui and gui.text then
        gui.text(2, 2, msg) -- 좌상단
    else
        print(msg)
    end
end

---------------------------
-- 2) 한글 → 영문 매핑 (사용자 제공 그대로)
---------------------------
local effect_kor_to_eng = {
    ["너는 맨손이다"] = "Arm confiscated",
    ["장갑 주기"] = "Arm",
    ["꿈을 꾸었습니다"] = "Ascension fist confiscated",
    ["옛다 승룡권"] = "Ascension fist",
    ["너는 맨몸이다"] = "Body confiscated",
    ["옷 주기"] = "Body",
    ["오랜지병"] = "Die",
    ["안심하세요 병원입니다"] = "HP MAX",
    ["너는 민머리다"] = "Head confiscated",
    ["모자 주기"] = "Head",
    ["불멸자"] = "I am immortal",
    ["메뉴를 소중히"] = "I didn't value the menu.",
    ["무한 점프"] = "Infinite Jump",
    ["무한 연사"] = "Infinite burst mode",
    ["지금부터 맨발이다"] = "Legs confiscated",
    ["신발 주기"] = "Legs",
    ["뎀프시 롤"] = "Random manipulation",
    ["엑스는 겁쟁이"] = "Retreat, retreat!!",
    ["기를 모아야합니다"] = "We must gather our strength.",
    ["너모든옷압수"] = "all arrmo confiscated",
    ["풀세트만 주기"] = "all arrmo",
    ["멀리 뛰기"] = "long jump",
    ["내가 죽는다구요!!"] = "low life",
    ["점프 금지"] = "no Jump",
    ["5달러"] = "HP five slots",
    ["더 월드"] = "No keyboards allowed",
    ["엑스는 앞만봐"] = "Charge forward!!",
    ["대쉬 금지"] = "Dash Stop",
    ["보스는 한발로 충분해"] = "Kill Boss 1 Hit",
}

---------------------------
-- 3) 유틸 / 파일 I/O / 파서
---------------------------
local function trim(s) return s and (s:gsub("^%s+",""):gsub("%s+$","")) or s end

local function read_file(path)
    local f = io.open(path, "r"); if not f then return nil end
    local c = f:read("*a"); f:close()
    if c and c ~= "" then return c end
    return nil
end

local function clear_file(which)
    if which == "txt" then
        local f = io.open(FILE_TXT, "w"); if f then f:write(""); f:close() end
    elseif which == "lua" then
        local f2 = io.open(FILE_LUA, "w"); if f2 then f2:write(""); f2:close() end
    end
end

-- effect_name= 값에서 한 개 또는 여러 개 추출
-- 여러 개 구분자 지원: ',', '+', '|'
local function parse_effect_list(content)
    local raw = content:match("effect_name%s*=%s*\"([^\r\n\"]+)\"")
             or content:match("effect_name%s*=%s*'([^\r\n']+)'")
             or content:match("effect_name%s*=%s*([^\r\n;]+)")
    raw = trim(raw)
    if not raw or raw == "" then return {} end
    local list = {}
    -- 분할
    for token in raw:gmatch("[^,+|]+") do
        token = trim(token)
        if token ~= "" then table.insert(list, token) end
    end
    return list
end

local function read_vote()
    local content = read_file(FILE_TXT)
    local from = content and "txt" or nil
    if not content then
        content = read_file(FILE_LUA)
        from = content and "lua" or nil
    end
    if not content then return nil, nil end
    local names = parse_effect_list(content)
    if #names == 0 then return nil, nil end
    return names, from
end

---------------------------
-- 4) 코루틴 스케줄러 + 중복쿨다운
---------------------------
local active = {}  -- 실행중 코루틴
local recent = {}  -- { [eng_name] = 남은쿨다운프레임 }

-- emu.frameadvance를 안전하게 오버라이드: 실제 호출 대신 yield
do
    local real_emu = emu
    if not emu then emu = {} end
    emu._real_frameadvance = real_emu and real_emu.frameadvance or nil
    emu.frameadvance = function() coroutine.yield() end
end

local function start_effect(fn)
    local co = coroutine.create(fn)
    table.insert(active, co)
end

local function tick_coroutines()
    if #active > 0 then
        local still = {}
        for _, co in ipairs(active) do
            local ok, err = coroutine.resume(co)
            if not ok then
                log("[Chaos] 효과 코루틴 오류: " .. tostring(err))
            elseif coroutine.status(co) ~= "dead" then
                table.insert(still, co)
            end
        end
        active = still
    end
    -- 쿨다운 감소
    for k, v in pairs(recent) do
        recent[k] = v - 1
        if recent[k] <= 0 then recent[k] = nil end
    end
end

---------------------------
-- 5) 메모리 헬퍼(아머 비트)
---------------------------
local function set_bit(v, n)
    return bit.bor(v, bit.lshift(1, n)) % 256
end
local function clear_bit(v, n)
    return bit.band(v, bit.bnot(bit.lshift(1, n))) % 256
end

local function wear_part(bitn)   -- 해당 부위 장착
    local v = memory.readbyte(ARMOR_ADDR)
    memory.writebyte(ARMOR_ADDR, set_bit(v, bitn))
end
local function take_part(bitn)   -- 해당 부위 해제(압수)
    local v = memory.readbyte(ARMOR_ADDR)
    memory.writebyte(ARMOR_ADDR, clear_bit(v, bitn))
end

---------------------------
-- 6) 효과 구현 (기존 + 개선)
---------------------------
local effects = {
    -- 팔(장갑)
    ["Arm confiscated"] = function() take_part(BIT_ARM) end,
    ["Arm"] = function() wear_part(BIT_ARM) end,

    -- 승천주먹(원펀맨) 세트
    ["Ascension fist confiscated"] = function()
        memory.writebyte(0x1FD1, 16)
        memory.writebyte(0x1FD6, 0)
        memory.writebyte(0x1FD7, 0)
        memory.writebyte(0x1FD8, 0)
        memory.writebyte(0x1FD0, 0)
        memory.writebyte(0x1FD0, 0)
        memory.writebyte(0x1FB1, bit.band(memory.readbyte(0x1FB1), 0x7F))
    end,

    ["Ascension fist"] = function()
        memory.writebyte(0x1FD1, 32)
        memory.writebyte(0x1FD6, 0xFF)
        memory.writebyte(0x1FD7, 0xFF)
        memory.writebyte(0x1FD8, 0xFF)
        memory.writebyte(0x1FD0, 0xFF)
        memory.writebyte(0x1FB1, bit.bor(memory.readbyte(0x1FB1), 0x80))
    end,

    ["Kill Boss 1 Hit"] = function()
        local timer = 30 * 60
        while timer > 0 do
            -- 1/4: 7E0DBF01
            memory.writebyte(0x7E0DBF, 0x01)
            -- 2/4: 7E0D3F01
            memory.writebyte(0x7E0D3F, 0x01)
            -- 3/4: 7E0DFF01
            memory.writebyte(0x7E0DFF, 0x01)
            -- 4/4: 7E0D7F01
            memory.writebyte(0x7E0D7F, 0x01)
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    -- 몸통(보디)
    ["Body confiscated"] = function() take_part(BIT_BODY) end,
    ["Body"] = function() wear_part(BIT_BODY) end,

    -- 대시 멈춤
    ["Dash Stop"] = function()
        local timer = 60 * 60
        while timer > 0 do
            local jp = joypad.get(1); jp.A = false; joypad.set(1, jp)
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    ["Die"] = function()
        local timer = 60 * 60
        while timer > 0 do
            memory.writebyte(0x1F37, 255)
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    ["HP MAX"] = function() memory.writebyte(0x09FF, 32) end,

    -- 머리(헬멧)
    ["Head confiscated"] = function() take_part(BIT_HEAD) end,
    ["Head"] = function() wear_part(BIT_HEAD) end,

    ["I am immortal"] = function()
        local timer = 30 * 60
        while timer > 0 do
            memory.writebyte(0x09FF, 32)
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    ["HP five slots"] = function()
    -- 현재 최대 체력칸(0x1FD1) 백업
    local original_max = memory.readbyte(0x1FD1)

    -- 최대 체력칸을 5칸으로 제한
    memory.writebyte(0x1FD1, 5)

    -- (선택) 현재 체력이 새 최대치보다 크면 살짝 깎아 정합 맞추기
    -- 주석 해제해서 쓰고 싶으면 아래 3줄 주석 제거
    -- local cur = memory.readbyte(0x09FF)
    -- if cur > 5 then memory.writebyte(0x09FF, 5) end

    -- 60초 유지 (60fps 기준 3600프레임)
    local timer = 60 * 60
    while timer > 0 do
        -- 혹시 게임 내부 이벤트가 값을 되돌려도 유지되도록 주기적으로 강제
        if timer % 8 == 0 then
            memory.writebyte(0x1FD1, 5)
        end
        emu.frameadvance()
        timer = timer - 1
    end

    -- 원래 최대 체력칸으로 복구
    memory.writebyte(0x1FD1, original_max)
end,

    ["I didn't value the menu."] = function()
        local timer = 60 * 60
        while timer > 0 do
            if timer % 300 == 0 then memory.writebyte(0x1F37, 1) end
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    ["Infinite Jump"] = function()
        local timer = 30 * 60
        while timer > 0 do
            joypad.set(1, {B=true}); emu.frameadvance(); timer = timer - 1
            joypad.set(1, {B=false}); emu.frameadvance(); timer = timer - 1
        end
    end,

    ["Infinite burst mode"] = function()
        local timer = 30 * 60
        while timer > 0 do
            joypad.set(1, {Y=true}); emu.frameadvance(); timer = timer - 1
            joypad.set(1, {Y=false}); emu.frameadvance(); timer = timer - 1
        end
    end,

    -- 다리(부츠)
    ["Legs confiscated"] = function() take_part(BIT_LEG) end,
    ["Legs"] = function() wear_part(BIT_LEG) end,

   ["No keyboards allowed"] = function()
        local timer = 60 * 60
        while timer > 0 do
            joypad.set(1, {right=false})
            joypad.set(1, {left=false})
            joypad.set(1, {A=false})
            joypad.set(1, {B=false})
            joypad.set(1, {Y=false})
            joypad.set(1, {L=false})
            joypad.set(1, {R=false})
            emu.frameadvance()
            timer = timer - 1
        end
    end,


    ["Random manipulation"] = function()
        math.randomseed(os.time())
        local timer = 60 * 60
        while timer > 0 do
            local jp = {}
            if math.random() < 0.5 then jp.left  = true end
            if math.random() < 0.5 then jp.right = true end
            if math.random() < 0.5 then jp.A     = true end
            if math.random() < 0.5 then jp.B     = true end
            joypad.set(1, jp)
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    ["Retreat, retreat!!"] = function()
        local timer = 60 * 60
        while timer > 0 do
            joypad.set(1, {left=true})
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    ["Charge forward!!"] = function()
        local timer = 60 * 60
        while timer > 0 do
            joypad.set(1, {right=true})
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    ["We must gather our strength."] = function()
        local timer = 60 * 60
        while timer > 0 do
            joypad.set(1, {Y=true})
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    -- 전체 세트
    ["all arrmo confiscated"] = function() memory.writebyte(ARMOR_ADDR, 0) end,
    ["all arrmo"] = function() memory.writebyte(ARMOR_ADDR, 0x0F) end, -- 0000 1111

    ["long jump"] = function()
        local timer = 60 * 60
        while timer > 0 do
            if timer % 150 == 0 then
                for i = 1, 30 do
                    joypad.set(1, {A=true, B=true})
                    emu.frameadvance()
                    timer = timer - 1
                end
                joypad.set(1, {})
                emu.frameadvance()
                timer = timer - 1
            end
            emu.frameadvance()
            timer = timer - 1
        end
    end,

    ["low life"] = function() memory.writebyte(0x09FF, 1) end,

    ["no Jump"] = function()
        local timer = 60 * 60
        while timer > 0 do
            local jp = joypad.get(1); jp.B = false; joypad.set(1, jp)
            emu.frameadvance()
            timer = timer - 1
        end
    end,
}

---------------------------
-- 7) 프레임 콜백: 매 프레임 투표파일 확인 & 코루틴 스텝
---------------------------
local function on_frame()
    -- 7-1) 파일 확인
    local kor_list, from = read_vote()
    if kor_list and #kor_list > 0 then
        -- 한글 → 영문 변환, 유효한 것만 남김
        local eng_list = {}
        for _, kor_name in ipairs(kor_list) do
            local eng = effect_kor_to_eng[kor_name]
            if not eng then
                log("[Chaos] 알 수 없는 효과: " .. tostring(kor_name))
            else
                table.insert(eng_list, eng)
            end
        end

        -- 유효 항목 있으면 시작
        if #eng_list > 0 then
            for _, eng in ipairs(eng_list) do
                if not recent[eng] then
                    log("[Chaos] 효과 시작: " .. tostring(eng))
                    start_effect(function()
                        effects[eng]()  -- 내부의 emu.frameadvance()는 yield로 동작
                    end)
                    recent[eng] = EFFECT_COOLDOWN_FRAMES
                else
                    -- 쿨다운 중이면 무시(스팸 방지)
                    -- log("[Chaos] 쿨다운 중 스킵: " .. tostring(eng))
                end
            end
        end

        -- 중복 실행 방지를 위해 읽은 파일만 비우기
        if from then clear_file(from) end
    end

    -- 7-2) 실행 중인 효과 한 스텝 진행 + 쿨다운 감소
    tick_coroutines()
end

-- 콜백 등록: emu.registerbefore가 있으면 우선 사용, 없으면 gui.register
if emu and emu.registerbefore then
    emu.registerbefore(on_frame)
elseif gui and gui.register then
    gui.register(on_frame)
else
    log("[Chaos] 경고: register 콜백이 없어 폴백 루프 사용 (권장 X)")
    while true do
        on_frame()
    end
end

-- ✅ 스크립트 로드 후 즉시 한 번 파일 확인 및 실행 시도
on_frame()
