#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <array>
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

struct Handle {
    HANDLE h=nullptr;
    explicit Handle(HANDLE p=nullptr):h(p){}
    ~Handle(){if(h && h!=INVALID_HANDLE_VALUE) CloseHandle(h);}
    Handle(const Handle&)=delete; Handle& operator=(const Handle&)=delete;
};
struct CS {
    CRITICAL_SECTION cs;
    CS(){if(!InitializeCriticalSectionEx(&cs,0,0)) throw std::runtime_error("InitializeCriticalSectionEx failed");}
    ~CS(){DeleteCriticalSection(&cs);}
    CS(const CS&)=delete;
};
struct Guard {
    CS& c;
    explicit Guard(CS& s):c(s){EnterCriticalSection(&c.cs);}
    ~Guard(){LeaveCriticalSection(&c.cs);}
};
[[noreturn]] void apiError(const char* api){
    DWORD code=GetLastError(); char* text=nullptr;
    FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER|FORMAT_MESSAGE_FROM_SYSTEM|FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr,code,0,reinterpret_cast<char*>(&text),0,nullptr);
    std::string message=std::string(api)+" GetLastError="+std::to_string(code)+" "+(text?text:"");
    if(text) LocalFree(text);
    throw std::runtime_error(message);
}
int64_t counter(){LARGE_INTEGER t;if(!QueryPerformanceCounter(&t))apiError("QueryPerformanceCounter");return t.QuadPart;}
struct Config {
    int workers=2,operations=500,capacity=32,work=20000,statsWork=2000,maxMs=30000;
    uint32_t seed=11573; bool separate=false,quiet=false; int inject=-1;
};
struct StageStats {uint64_t count=0,checksum=0;};
struct Local {
    int id=0,stage=0; uint64_t completed=0;
    double statsWait=0,statsHold=0,queueWait=0,busy=0,maxStatsWait=0;
};
struct Job {int id=-1;uint64_t value=0;};
struct Context;
struct Queue {
    CS lock; Handle items,spaces; std::deque<Job> jobs; size_t capacity,peak=0;
    Queue(int cap):items(CreateSemaphoreW(nullptr,0,cap,nullptr)),spaces(CreateSemaphoreW(nullptr,cap,cap,nullptr)),capacity(cap){
        if(!items.h || !spaces.h)apiError("CreateSemaphoreW");
    }
    void push(Context&,Job,double&);
    Job pop(Context&,double&);
};
struct Context {
    Config cfg; Handle cancel{CreateEventW(nullptr,TRUE,FALSE,nullptr)};
    CS logger,globalStats; std::array<CS,3> stageLocks;
    std::array<StageStats,3> stats{};
    std::array<std::unique_ptr<Queue>,4> queues;
    std::vector<LONG> states; std::vector<uint64_t> results;
    std::atomic<int> errors{0},violations{0},timeouts{0};
    int64_t begin=0; double frequency=0; std::string firstError;
    explicit Context(Config c):cfg(c),states(c.operations,0),results(c.operations,0){
        if(!cancel.h)apiError("CreateEventW");
        LARGE_INTEGER f;if(!QueryPerformanceFrequency(&f))apiError("QueryPerformanceFrequency");frequency=double(f.QuadPart);
        for(auto& q:queues)q=std::make_unique<Queue>(cfg.capacity);
        begin=counter();
    }
    double ms(int64_t ticks)const{return 1000.0*ticks/frequency;}
    void log(int id,const std::string& event,int job=-1,bool force=false){
        if(cfg.quiet && !force)return;
        Guard g(logger);
        std::cout<<"["<<ms(counter()-begin)<<" ms] PARTICIPANT="<<id<<" PID="<<GetCurrentProcessId()
                 <<" TID="<<GetCurrentThreadId()<<" "<<event;
        if(job>=0)std::cout<<" REQUEST="<<job;
        std::cout<<std::endl;
    }
    void fail(const std::string& message){
        ++errors;
        {Guard g(logger);if(firstError.empty())firstError=message;}
        if(!SetEvent(cancel.h))apiError("SetEvent");
    }
    DWORD remaining(){
        double left=cfg.maxMs-ms(counter()-begin);
        return left>0 ? static_cast<DWORD>(std::max(1.0,left)) : 0;
    }
    void wait(HANDLE h){
        HANDLE hs[]={cancel.h,h};DWORD w=WaitForMultipleObjects(2,hs,FALSE,remaining());
        if(w==WAIT_OBJECT_0+1)return;
        if(w==WAIT_TIMEOUT){++timeouts;throw std::runtime_error("EXPERIMENT_TIMEOUT");}
        if(w==WAIT_OBJECT_0)throw std::runtime_error("CANCELLED");
        apiError("WaitForMultipleObjects");
    }
    void invariant(bool ok,const char* message){if(!ok){++violations;throw std::runtime_error(message);}}
};
void release(HANDLE h){if(!ReleaseSemaphore(h,1,nullptr))apiError("ReleaseSemaphore");}
void Queue::push(Context& c,Job job,double& wait){
    auto t=counter();c.wait(spaces.h);wait+=c.ms(counter()-t);
    {Guard guard(lock);c.invariant(jobs.size()<capacity,"queue overflow");jobs.push_back(job);peak=std::max(peak,jobs.size());}
    release(items.h);
}
Job Queue::pop(Context& c,double& wait){
    auto t=counter();c.wait(items.h);wait+=c.ms(counter()-t);
    Job job;
    {Guard guard(lock);c.invariant(!jobs.empty(),"queue underflow");job=jobs.front();jobs.pop_front();}
    release(spaces.h);return job;
}
uint64_t process(uint64_t value,int stage,int iterations){
    value^=0x9e3779b97f4a7c15ull+static_cast<uint64_t>(stage);
    for(int i=0;i<iterations;++i)value=value*6364136223846793005ull+1442695040888963407ull;
    return value;
}
uint64_t initial(int id,uint32_t seed){return (uint64_t(seed)<<32)^uint64_t(id+1);}
struct Worker {Context* c;Local local;};
DWORD WINAPI workerMain(void* p){
    auto& w=*static_cast<Worker*>(p);auto& c=*w.c;auto& l=w.local;
    try{
        c.log(l.id,"START STAGE="+std::to_string(l.stage+1));
        for(;;){
            c.log(l.id,"WAITING_QUEUE");Job job=c.queues[l.stage]->pop(c,l.queueWait);
            if(job.id<0)break;
            auto busyBegin=counter();c.log(l.id,"WORKING",job.id);
            c.invariant(job.id<c.cfg.operations,"request id out of range");
            LONG expected=2*l.stage;
            c.invariant(InterlockedCompareExchange(&c.states[job.id],expected+1,expected)==expected,"duplicate/out-of-order stage start");
            if(c.cfg.inject==job.id && l.stage==1)throw std::runtime_error("INJECTED_WORKER_FAILURE");
            job.value=process(job.value,l.stage,c.cfg.work);
            CS& lock=c.cfg.separate?c.stageLocks[l.stage]:c.globalStats;
            c.log(l.id,"WAITING_STATS RESOURCE="+std::to_string(c.cfg.separate?l.stage:0),job.id);
            auto before=counter();
            {
                Guard guard(lock);auto acquired=counter();double waited=c.ms(acquired-before);
                l.statsWait+=waited;l.maxStatsWait=std::max(l.maxStatsWait,waited);
                auto& stat=c.stats[l.stage];++stat.count;
                // A bounded synthetic checksum update exposes contention. Identical in both modes.
                uint64_t audit=job.value;
                for(int i=0;i<c.cfg.statsWork;++i)audit=audit*2862933555777941757ull+3037000493ull;
                stat.checksum+=audit;
                l.statsHold+=c.ms(counter()-acquired);
            }
            c.log(l.id,"RELEASED_STATS",job.id);
            c.invariant(InterlockedCompareExchange(&c.states[job.id],expected+2,expected+1)==expected+1,"stage finish state");
            ++l.completed;l.busy+=c.ms(counter()-busyBegin);
            c.queues[l.stage+1]->push(c,job,l.queueWait);
        }
        c.log(l.id,"FINISHED");return 0;
    }catch(const std::exception& e){c.log(l.id,std::string("ERROR ")+e.what(),-1,true);c.fail(e.what());return 1;}
}
struct Collector {Context* c;uint64_t completed=0;double queueWait=0;};
DWORD WINAPI collectorMain(void* p){
    auto& x=*static_cast<Collector*>(p);auto& c=*x.c;int id=3*c.cfg.workers+1;
    try{
        c.log(id,"START COLLECTOR");
        for(;;){
            Job job=c.queues[3]->pop(c,x.queueWait);if(job.id<0)break;
            c.invariant(job.id<c.cfg.operations,"collector id out of range");
            c.invariant(InterlockedCompareExchange(&c.states[job.id],7,6)==6,"duplicate/incomplete result");
            c.results[job.id]=job.value;++x.completed;c.log(id,"COLLECTED",job.id);
        }
        c.log(id,"FINISHED");return 0;
    }catch(const std::exception& e){c.log(id,std::string("ERROR ")+e.what(),-1,true);c.fail(e.what());return 1;}
}
int integer(const std::string& s,int max){
    if(s.empty()||s.find_first_not_of("0123456789")!=std::string::npos)throw std::runtime_error("invalid integer");
    auto n=std::stoull(s);if(n>static_cast<unsigned>(max))throw std::runtime_error("integer out of range");return int(n);
}
int main(int argc,char** argv){
    try{
        Config cfg;
        for(int i=1;i<argc;++i){
            std::string a=argv[i];if(a=="--quiet"){cfg.quiet=true;continue;}
            if(a=="--help"){
                std::cout<<"Variant22: --mode global|stage --workers 1..16 --operations 1..100000\n"
                  "--individual (1045 operations) --capacity 1..4096 --seed N --work 0..1000000\n"
                  "--stats-work 0..1000000 --max-ms 1..600000 --inject-failure ID --quiet\n";return 0;
            }
            if(a=="--individual"){cfg.operations=1045;continue;}
            if(i+1==argc)throw std::runtime_error("missing option value");
            std::string v=argv[++i];
            if(a=="--mode"){if(v!="global"&&v!="stage")throw std::runtime_error("invalid mode");cfg.separate=v=="stage";}
            else if(a=="--workers")cfg.workers=integer(v,16);
            else if(a=="--operations")cfg.operations=integer(v,100000);
            else if(a=="--capacity")cfg.capacity=integer(v,4096);
            else if(a=="--seed")cfg.seed=integer(v,0x7fffffff);
            else if(a=="--work")cfg.work=integer(v,1000000);
            else if(a=="--stats-work")cfg.statsWork=integer(v,1000000);
            else if(a=="--max-ms")cfg.maxMs=integer(v,600000);
            else if(a=="--inject-failure")cfg.inject=integer(v,99999);
            else throw std::runtime_error("unknown option: "+a);
        }
        if(!cfg.workers||!cfg.operations||!cfg.capacity||!cfg.maxMs)throw std::runtime_error("positive parameters required");
        Context c(cfg);c.log(0,"START GENERATOR VARIANT=22 STUDENT_ID=109 SEED="+std::to_string(cfg.seed),-1,true);
        std::vector<Worker> workers(3*cfg.workers);std::vector<std::unique_ptr<Handle>> threads;
        Collector collector{&c};
        try{
            Handle* collectorThread=nullptr;
            threads.push_back(std::make_unique<Handle>(CreateThread(nullptr,0,collectorMain,&collector,0,nullptr)));
            if(!threads.back()->h)apiError("CreateThread collector");
            collectorThread=threads.back().get();
            for(int s=0;s<3;++s)for(int j=0;j<cfg.workers;++j){
                int index=s*cfg.workers+j;workers[index]={&c,{index+1,s}};
                threads.push_back(std::make_unique<Handle>(CreateThread(nullptr,0,workerMain,&workers[index],0,nullptr)));
                if(!threads.back()->h)apiError("CreateThread worker");
            }
            double generatorWait=0;
            for(int id=0;id<cfg.operations;++id){c.queues[0]->push(c,{id,initial(id,cfg.seed)},generatorWait);c.log(0,"CREATED",id);}
            c.log(0,"FINISHED_GENERATING");
            // Only the coordinator inserts stop tokens after the upstream group has joined.
            for(int s=0;s<3;++s){
                for(int j=0;j<cfg.workers;++j)c.queues[s]->push(c,{},generatorWait);
                for(int j=0;j<cfg.workers;++j)c.wait(threads[1+s*cfg.workers+j]->h);
            }
            c.queues[3]->push(c,{},generatorWait);c.wait(collectorThread->h);
        }catch(const std::exception& e){c.fail(e.what());}
        // After cancellation all semaphore waits wake; finite CPU operations release every CS.
        // Join before destroying queues, locks or thread arguments, including partial startup.
        for(auto& t:threads)if(t->h){if(WaitForSingleObject(t->h,INFINITE)==WAIT_FAILED)apiError("WaitForSingleObject join");}
        double elapsed=c.ms(counter()-c.begin);
        if(c.errors){std::cerr<<"FAILED errors="<<c.errors<<" violations="<<c.violations<<" timeouts="<<c.timeouts
            <<" reason="<<c.firstError<<std::endl;return 1;}
        // Sequential reference and final checks are outside the timed experiment.
        std::array<uint64_t,3> checksums{};
        c.invariant(collector.completed==static_cast<unsigned>(cfg.operations),"missing results");
        for(int i=0;i<cfg.operations;++i){
            c.invariant(c.states[i]==7,"unfinished request");uint64_t value=initial(i,cfg.seed);
            for(int s=0;s<3;++s){value=process(value,s,cfg.work);uint64_t audit=value;
                for(int n=0;n<cfg.statsWork;++n)audit=audit*2862933555777941757ull+3037000493ull;
                checksums[s]+=audit;}
            c.invariant(value==c.results[i],"sequential reference mismatch");
        }
        double wait=0,hold=0,busy=0,queueWait=0,maxWait=0;
        for(const auto& w:workers){const auto& l=w.local;wait+=l.statsWait;hold+=l.statsHold;busy+=l.busy;queueWait+=l.queueWait;maxWait=std::max(maxWait,l.maxStatsWait);
            std::cout<<"WORKER id="<<l.id<<" stage="<<l.stage+1<<" completed="<<l.completed
              <<" stats_wait_ms="<<l.statsWait<<" queue_wait_ms="<<l.queueWait<<" busy_ms="<<l.busy<<std::endl;}
        for(int s=0;s<3;++s){
            c.invariant(c.stats[s].count==static_cast<unsigned>(cfg.operations),"statistics count mismatch");
            c.invariant(c.stats[s].checksum==checksums[s],"statistics checksum mismatch");
        }
        for(auto& q:c.queues)c.invariant(q->jobs.empty(),"queue not drained");
        std::cout<<"SUMMARY mode="<<(cfg.separate?"stage":"global")<<" workers="<<cfg.workers
          <<" operations="<<cfg.operations<<" completed="<<collector.completed<<" rejected=0 seed="<<cfg.seed
          <<" capacity="<<cfg.capacity<<" work="<<cfg.work<<" stats_work="<<cfg.statsWork
          <<" elapsed_ms="<<elapsed<<" throughput="<<1000.0*cfg.operations/elapsed
          <<" stats_wait_ms="<<wait<<" mean_stats_wait_us="<<wait*1000/(3*cfg.operations)
          <<" max_stats_wait_ms="<<maxWait<<" stats_hold_ms="<<hold<<" queue_wait_ms="<<queueWait
          <<" utilization="<<busy/(3*cfg.workers*elapsed)<<" violations="<<c.violations<<" timeouts="<<c.timeouts
          <<" errors="<<c.errors<<" peak0="<<c.queues[0]->peak<<" peak1="<<c.queues[1]->peak
          <<" peak2="<<c.queues[2]->peak<<" peak3="<<c.queues[3]->peak<<std::endl;
        return 0;
    }catch(const std::exception& e){std::cerr<<"ERROR "<<e.what()<<std::endl;return 1;}
}
