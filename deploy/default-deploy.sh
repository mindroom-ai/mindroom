./deploy.py create default --matrix synapse --domain default.mindroom.chat --auth authelia
./deploy.py create try --matrix synapse --domain try.mindroom.chat --auth authelia
./deploy.py create alt --matrix tuwunel --domain alt.mindroom.chat --auth authelia
./deploy.py create test --matrix tuwunel --domain test.mindroom.chat
./deploy.py create test-2 --matrix tuwunel --domain test-2.mindroom.chat
./deploy.py create test-3 --matrix tuwunel --domain test-3.mindroom.chat
./deploy.py create test-4 --matrix tuwunel --domain test-4.mindroom.chat
./deploy.py create test-5 --matrix tuwunel --domain test-5.mindroom.chat

./deploy.py start alt
./deploy.py start default
./deploy.py start try
./deploy.py start --only-matrix test
./deploy.py start --only-matrix test-2
./deploy.py start --only-matrix test-3
./deploy.py start --only-matrix test-4
./deploy.py start --only-matrix test-5
